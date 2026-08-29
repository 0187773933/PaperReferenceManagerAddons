# syntax=docker/dockerfile:1
#
# prma server , containerized.
#
# The point of this image : the machine that RUNS the server never needs a copy
# of the source. The build clones
# https://github.com/0187773733/PaperReferenceManagerAddons itself , so the only
# two files that ever get copied to that machine are this Dockerfile and
# dockerRun.sh next to it. Re-running dockerRun.sh resolves the newest commit on
# the branch , rebuilds if it moved , and restarts the container -- see that
# script for the update check.
#
# WHAT THIS IMAGE CAN AND CANNOT DO
#
#   CAN : everything on the serving path -- ` prma server ` ( the dashboard at
#         GET / , the userscript endpoint POST /exists , /sort , /tiers , the
#         figure reports , /status , /errors ) , ` prma snapshot ` off the
#         mounted Zotero library , the OpenAlex cache update ( ` prma main ` )
#         and ` prma reindex ` . All of that is plain Python + requests.
#
#   CANNOT : the PDF pipeline -- yolo , ocr , images , methods , md -- and so
#         ` server --watch ` , which runs that suite per newly-added paper.
#         Those need torch ( doclayout-yolo , surya-ocr ) , paddlepaddle ,
#         onnxruntime ( rapidocr ) , opencv and playwright , none of which
#         publish musl wheels ; they cannot be installed on Alpine at all , at
#         any image size. Keep running the pipeline on the main machine and let
#         this box serve what it wrote ( output/ is a mount , so rsync-ing that
#         directory over is how the results get here ).
#
# The dependency filter that drops those packages lives in the source stage
# below.

ARG PYTHON_VERSION=3.10
ARG ALPINE_VERSION=3.21


###############################################################################
# 1. source :: clone the repo INSIDE the build
###############################################################################
FROM alpine:${ALPINE_VERSION} AS source

ARG GIT_REPO=https://github.com/0187773733/PaperReferenceManagerAddons.git
ARG GIT_REF=master
# The commit dockerRun.sh resolved just before calling build. Two jobs :
#   1. cache-bust : a build ARG that changes invalidates this layer , so a new
#      commit upstream forces a fresh clone instead of silently reusing the
#      cached one ( this is what makes "rerun the script and it updates" work ).
#   2. pin : the branch can move between the ls-remote and this clone , so we
#      check out the exact SHA that was resolved rather than whatever HEAD is
#      by the time the build gets here.
ARG GIT_SHA=

RUN apk add --no-cache git ca-certificates

WORKDIR /src
RUN set -eux ; \
	git clone --depth 1 --branch "${GIT_REF}" "${GIT_REPO}" app ; \
	cd app ; \
	if [ -n "${GIT_SHA}" ] && [ "$( git rev-parse HEAD )" != "${GIT_SHA}" ] ; then \
		git fetch --depth 1 origin "${GIT_SHA}" ; \
		git checkout -q FETCH_HEAD ; \
	fi ; \
	git rev-parse HEAD > /src/GIT_SHA ; \
	rm -rf .git

# requirements.txt in the repo is the FULL desktop set , the ML stack included.
# Filter it here rather than freezing a hand-written list in this file : a plain
# dependency added upstream then lands in the image on the next rebuild by
# itself , which is the whole point of cloning instead of copying.
#
# Dropped , and why :
#   doclayout-yolo surya-ocr        -> torch
#   paddlepaddle paddleocr          -> paddle
#   rapidocr-onnxruntime            -> onnxruntime
#   deskew                          -> scipy / opencv
#   playwright                      -> bundled Chromium ( Mendeley login only )
#   pikepdf pypdfium2 pymupdf Pillow-> PDF rasterizing , pipeline-only
#   huggingface_hub wordninja       -> OCR model download / word splitting
#   curl_cffi                       -> Mendeley API transport only
# None are imported by the serving path ( src/server/server.py and what it
# reaches ) , and every one of the first five is glibc-only anyway. If a route
# ever does need one back , delete it from this pattern and rebuild.
RUN set -eux ; \
	grep -vEi \
		'^[[:space:]]*(doclayout-yolo|surya-ocr|paddlepaddle|paddleocr|rapidocr-onnxruntime|deskew|playwright|pikepdf|pypdfium2|pymupdf|Pillow|huggingface_hub|wordninja|curl_cffi)([[:space:]<>=!~;#].*)?$' \
		/src/app/requirements.txt > /src/requirements-server.txt ; \
	echo "--- server dependency set ---" ; \
	cat /src/requirements-server.txt


###############################################################################
# 2. builder :: wheels , with the compilers that never reach the final image
###############################################################################
FROM python:${PYTHON_VERSION}-alpine AS builder

# Present so that a future dependency without a musl wheel can still build from
# source instead of failing the image. Nothing in the current set needs them.
RUN apk add --no-cache build-base musl-dev libffi-dev openssl-dev

COPY --from=source /src/requirements-server.txt /tmp/requirements-server.txt

# --prefix : everything lands under /install , which is copied onto /usr/local
# in the runtime stage. Same base image on both sides , so the interpreter and
# the ABI match.
RUN pip install --no-cache-dir --prefix=/install --upgrade pip setuptools wheel \
	&& pip install --no-cache-dir --prefix=/install -r /tmp/requirements-server.txt


###############################################################################
# 3. runtime
###############################################################################
FROM python:${PYTHON_VERSION}-alpine AS runtime

ARG GIT_REPO=https://github.com/0187773733/PaperReferenceManagerAddons.git
ARG GIT_REF=master
ARG GIT_SHA=

# Bind UID/GID to the host user that owns the mounted output/ directory.
# dockerRun.sh passes $(id -u)/$(id -g) , so a bind-mounted output/ is writable
# by the container without ever running as root or chmod 777-ing anything.
ARG APP_UID=1000
ARG APP_GID=1000

LABEL org.opencontainers.image.source="${GIT_REPO}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.title="prma-server" \
      org.opencontainers.image.description="Paper Reference Manager Addons :: HTTP server ( dashboard + exists endpoint )" \
      prma.git.ref="${GIT_REF}" \
      prma.git.sha="${GIT_SHA}"

# ca-certificates : OpenAlex / Crossref over TLS. tzdata : local timestamps on
# the dashboard. That is the whole OS-package surface.
RUN apk add --no-cache ca-certificates tzdata

COPY --from=builder /install /usr/local
COPY --from=source  /src/app /app
COPY --from=source  /src/GIT_SHA /app/.git-sha

# Non-root , no shell , no home to write to , no sudo installed to escalate with.
RUN set -eux ; \
	if ! awk -F: -v g="${APP_GID}" '$3==g{f=1} END{exit !f}' /etc/group ; then \
		addgroup -g "${APP_GID}" prma ; \
	fi ; \
	APP_GROUP="$( awk -F: -v g="${APP_GID}" '$3==g{print $1; exit}' /etc/group )" ; \
	if ! awk -F: -v u="${APP_UID}" '$3==u{f=1} END{exit !f}' /etc/passwd ; then \
		adduser -D -H -u "${APP_UID}" -G "${APP_GROUP}" -h /home/prma -s /sbin/nologin prma ; \
	fi ; \
	install -d -m 0755 -o "${APP_UID}" -g "${APP_GID}" /home/prma /app/output

# Byte-compile at build time. The container runs with a read-only root
# filesystem ( see dockerRun.sh ) , so __pycache__ can't be written at import ;
# doing it here means the read-only run doesn't pay the recompile every start.
RUN python -m compileall -q /app/src /app/main.py || true

# prma resolves --output / --config / --searches relative to the CURRENT
# directory , so every invocation has to start in /app. The wrapper enforces
# that no matter what the container is started with.
RUN printf '%s\n' \
	'#!/bin/sh' \
	'cd /app' \
	'exec python main.py "$@"' \
	> /usr/local/bin/prma \
	&& chmod 0755 /usr/local/bin/prma

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/prma \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=9371

# 127.0.0.1 ( the CLI default ) would be unreachable from outside the container ,
# hence SERVER_HOST=0.0.0.0 above. The container is NOT exposed to the network
# by that : dockerRun.sh publishes the port to 127.0.0.1 on the host , so
# reaching it from another machine takes an explicit BIND_ADDR=0.0.0.0.
EXPOSE 9371

USER ${APP_UID}:${APP_GID}
WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
	CMD wget -q -O /dev/null "http://127.0.0.1:${SERVER_PORT}/api/version" || exit 1

ENTRYPOINT [ "prma" ]
CMD [ "server" ]
