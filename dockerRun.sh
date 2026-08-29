#!/usr/bin/env bash
#
# prma server , on a machine that has no copy of the source.
#
# What it does , every time you run it :
#   1. asks GitHub for the newest commit on the branch
#   2. compares that to the commit baked into the image already on this machine
#   3. rebuilds ( the Dockerfile clones the repo itself ) if they differ
#   4. replaces the running container with one from that image
#
# So updating the server is : ./dockerRun.sh . Nothing gets copied over by hand.
# Only this file and the Dockerfile beside it ever need to live on the box.
#
# Everything below is overridable from the environment , e.g.
#   ZOTERO_DIR=/mnt/zotero HOST_PORT=8080 ./dockerRun.sh
#
# Arguments that aren't one of the flags in usage() are passed straight through
# to prma , so ./dockerRun.sh --manager mendeley works , as does
# ./dockerRun.sh --exists for minimal mode.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[ 0 ]}" )" && pwd )"

# --- where the code comes from ----------------------------------------------
GIT_REPO="${GIT_REPO:-https://github.com/0187773733/PaperReferenceManagerAddons.git}"
GIT_REF="${GIT_REF:-master}"

# --- docker names ------------------------------------------------------------
IMAGE="${IMAGE:-prma-server}"
CONTAINER="${CONTAINER:-prma-server}"

# --- what to publish ---------------------------------------------------------
# Loopback by default : the dashboard has no login of any kind , so it is not a
# thing to hang off a public interface. Reach it from another machine over an
# SSH tunnel ( ssh -N -L 9371:127.0.0.1:9371 user@thisbox ) rather than by
# setting BIND_ADDR=0.0.0.0 , unless the box is already behind something that
# authenticates.
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
HOST_PORT="${HOST_PORT:-9371}"

# --- what to mount -----------------------------------------------------------
# ZOTERO_DIR  : the copied Zotero data directory. Needs zotero.sqlite AND the
#               storage/ folder next to it ( that's where the PDFs live , and
#               where the dashboard's PDF / figure links read from ). Mounted
#               READ-ONLY : prma byte-copies the sqlite into output/cache before
#               reading it , so it never writes here.
# DATA_DIR    : this machine's writable state -- output/ , i.e. the papers DB ,
#               the OpenAlex cache , the dashboard index , the /sort and /tiers
#               boards. SURVIVES rebuilds ; it's the one directory worth backing
#               up. rsync the main machine's output/ into DATA_DIR/output to
#               bring the PDF-pipeline results ( OCR text , figure crops , md )
#               over , since this image can't produce them ( see the Dockerfile ).
# CONFIG_FILE : config/config.yaml -- gitignored upstream , so the clone has no
#               copy and it has to come from here. Mounted read-only over just
#               that one path , leaving the repo's own config/methods.py etc.
#               in place.
ZOTERO_DIR="${ZOTERO_DIR:-${HOME}/Zotero}"
DATA_DIR="${DATA_DIR:-${SCRIPT_DIR}/data}"
CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/config.yaml}"

# --- misc --------------------------------------------------------------------
RESTART_POLICY="${RESTART_POLICY:-unless-stopped}"
MEMORY_LIMIT="${MEMORY_LIMIT:-2g}"
# The container runs with a read-only root filesystem. Set READ_ONLY_ROOTFS=0 if
# some future code path needs to write outside /app/output and /tmp.
READ_ONLY_ROOTFS="${READ_ONLY_ROOTFS:-1}"

FORCE_REBUILD=0
SKIP_UPDATE=0

usage() {
	cat <<'USAGE'
usage : ./dockerRun.sh [ flags ] [ -- ] [ prma args ... ]

  ( no flags )      check for updates , rebuild if needed , (re)start the server
  --rebuild         rebuild even when the commit hasn't moved
  --no-update       don't contact GitHub ; just restart the image already here
  --stop            stop and remove the container , leave the image
  --logs            follow the container's logs
  --status          show image commit , container state , health
  --shell           open a shell in a throwaway container ( debugging )
  --help            this

  anything else is passed to prma , after ` server ` , e.g.
      ./dockerRun.sh --exists              # minimal userscript-only mode
      ./dockerRun.sh --manager mendeley

environment ( defaults in parentheses ) :
  ZOTERO_DIR  ( ~/Zotero )       copied Zotero data dir ( zotero.sqlite + storage/ )
  DATA_DIR    ( ./data )         writable state ; DATA_DIR/output is the mount
  CONFIG_FILE ( ./config.yaml )  your config.yaml
  BIND_ADDR   ( 127.0.0.1 )      host interface to publish on
  HOST_PORT   ( 9371 )           host port
  GIT_REF     ( master )         branch to track
USAGE
}

say()  { printf '\033[1;36m::\033[0m %s\n' "$*" ; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2 ; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2 ; exit 1 ; }

# --- flags -------------------------------------------------------------------
PRMA_ARGS=()
while [ $# -gt 0 ] ; do
	case "$1" in
		--rebuild )   FORCE_REBUILD=1 ; shift ;;
		--no-update ) SKIP_UPDATE=1   ; shift ;;
		--help|-h )   usage ; exit 0 ;;
		--stop )
			docker rm -f "$CONTAINER" >/dev/null 2>&1 && say "stopped ${CONTAINER}" \
				|| say "${CONTAINER} wasn't running"
			exit 0 ;;
		--logs )
			exec docker logs -f --tail 200 "$CONTAINER" ;;
		--status )
			docker image inspect --format \
				'image  {{ .RepoTags }}{{ "\n" }}commit {{ index .Config.Labels "prma.git.sha" }}{{ "\n" }}built  {{ .Created }}' \
				"${IMAGE}:latest" 2>/dev/null || say "no ${IMAGE}:latest image yet"
			docker ps -a --filter "name=^/${CONTAINER}$" \
				--format 'state  {{ .Status }}{{ "\n" }}ports  {{ .Ports }}' 2>/dev/null || true
			exit 0 ;;
		--shell )
			exec docker run --rm -it --entrypoint /bin/sh "${IMAGE}:latest" ;;
		-- )          shift ; PRMA_ARGS+=( "$@" ) ; break ;;
		* )           PRMA_ARGS+=( "$1" ) ; shift ;;
	esac
done

# --- preflight ---------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker isn't installed / isn't on PATH"
docker info >/dev/null 2>&1 || die "can't talk to the docker daemon ( is it running ? )"

[ "$( id -u )" -ne 0 ] || die "run this as your normal user , not root -- the image is built to run as \$(id -u):\$(id -g)"

[ -d "$ZOTERO_DIR" ] || die "ZOTERO_DIR does not exist : ${ZOTERO_DIR}"
[ -f "${ZOTERO_DIR}/zotero.sqlite" ] || \
	die "no zotero.sqlite in ${ZOTERO_DIR} -- point ZOTERO_DIR at the copied Zotero data directory"
[ -d "${ZOTERO_DIR}/storage" ] || \
	warn "no storage/ in ${ZOTERO_DIR} -- the library's PDFs aren't here , so PDF / figure links will 404"

mkdir -p "${DATA_DIR}/output"

MOUNT_CONFIG=()
if [ -f "$CONFIG_FILE" ] ; then
	MOUNT_CONFIG=( -v "${CONFIG_FILE}:/app/config/config.yaml:ro" )
else
	warn "no config.yaml at ${CONFIG_FILE} -- serving anyway , but the OpenAlex refresh will log a failure every cycle."
	warn "copy config/config.example.yaml from the repo , fill it in , save it there."
fi

# --- what's the newest commit ? ----------------------------------------------
# git ls-remote when git is here ; otherwise GitHub's API , which returns the
# bare SHA for this Accept header. Both are a single request , no clone.
resolve_remote_sha() {
	if command -v git >/dev/null 2>&1 ; then
		git ls-remote "$GIT_REPO" "refs/heads/${GIT_REF}" 2>/dev/null | awk 'NR==1{ print $1 }'
		return
	fi
	local slug url
	slug="$( printf '%s' "$GIT_REPO" | sed -E 's#^https?://github\.com/##; s#\.git$##' )"
	url="https://api.github.com/repos/${slug}/commits/${GIT_REF}"
	if command -v curl >/dev/null 2>&1 ; then
		curl -fsSL -H 'Accept: application/vnd.github.sha' "$url" 2>/dev/null
	elif command -v wget >/dev/null 2>&1 ; then
		wget -qO- --header='Accept: application/vnd.github.sha' "$url" 2>/dev/null
	fi
}

# One GET , exit status only , with whichever http client the host has ; 2 means
# it has neither , which the wait at the bottom treats as "don't wait".
http_ok() {
	if command -v curl >/dev/null 2>&1 ; then
		curl -fsS -o /dev/null "$1" 2>/dev/null
	elif command -v wget >/dev/null 2>&1 ; then
		wget -qO /dev/null "$1" 2>/dev/null
	else
		return 2
	fi
}

LOCAL_SHA="$( docker image inspect --format '{{ index .Config.Labels "prma.git.sha" }}' \
	"${IMAGE}:latest" 2>/dev/null || true )"

REMOTE_SHA=""
if [ "$SKIP_UPDATE" -eq 0 ] ; then
	say "checking ${GIT_REPO} ( ${GIT_REF} ) for updates"
	REMOTE_SHA="$( resolve_remote_sha || true )"
fi

if [ -z "$REMOTE_SHA" ] ; then
	# Offline , or --no-update. Fine as long as something is already built.
	[ -n "$LOCAL_SHA" ] || die "no image built yet and can't reach ${GIT_REPO} -- need network for the first build"
	[ "$SKIP_UPDATE" -eq 1 ] || warn "couldn't reach GitHub ; running the image already here"
	REMOTE_SHA="$LOCAL_SHA"
	FORCE_REBUILD=0
fi

# --- build if the commit moved ( or we were told to ) ------------------------
NEED_BUILD=0
if [ -z "$LOCAL_SHA" ] ; then
	say "no ${IMAGE} image yet -- first build"
	NEED_BUILD=1
elif [ "$LOCAL_SHA" != "$REMOTE_SHA" ] ; then
	say "update : ${LOCAL_SHA:0:12} -> ${REMOTE_SHA:0:12}"
	NEED_BUILD=1
elif [ "$FORCE_REBUILD" -eq 1 ] ; then
	say "already at ${LOCAL_SHA:0:12} -- rebuilding anyway ( --rebuild )"
	NEED_BUILD=1
else
	say "already at ${LOCAL_SHA:0:12} -- no rebuild needed"
fi

if [ "$NEED_BUILD" -eq 1 ] ; then
	# Empty build context on purpose. The Dockerfile COPYs nothing from the
	# context ( it clones instead ) , and using this directory would upload
	# DATA_DIR -- gigabytes of output/ -- to the daemon on every build.
	BUILD_CTX="$( mktemp -d )"
	trap 'rm -rf "$BUILD_CTX"' EXIT

	docker build \
		--pull \
		--file "${SCRIPT_DIR}/Dockerfile" \
		--build-arg GIT_REPO="$GIT_REPO" \
		--build-arg GIT_REF="$GIT_REF" \
		--build-arg GIT_SHA="$REMOTE_SHA" \
		--build-arg APP_UID="$( id -u )" \
		--build-arg APP_GID="$( id -g )" \
		--tag "${IMAGE}:${REMOTE_SHA}" \
		--tag "${IMAGE}:latest" \
		"$BUILD_CTX"

	rm -rf "$BUILD_CTX"
	trap - EXIT
	say "built ${IMAGE}:${REMOTE_SHA:0:12}"
fi

# --- (re)start ---------------------------------------------------------------
# Always recreated , never restarted in place : that's what makes a changed
# mount / port / flag take effect from one run of this script to the next.
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

SECURITY_ARGS=(
	# No root , no sudo in the image , and no way to gain privileges via setuid.
	--security-opt no-new-privileges
	# The server binds a port and reads files. It needs none of Linux's
	# capabilities to do that.
	--cap-drop ALL
	--pids-limit 512
	--memory "$MEMORY_LIMIT"
)
if [ "$READ_ONLY_ROOTFS" -eq 1 ] ; then
	SECURITY_ARGS+=(
		--read-only
		--tmpfs /tmp:rw,noexec,nosuid,size=64m
		--tmpfs /home/prma:rw,noexec,nosuid,size=16m
	)
fi

docker run \
	--detach \
	--name "$CONTAINER" \
	--restart "$RESTART_POLICY" \
	"${SECURITY_ARGS[@]}" \
	--publish "${BIND_ADDR}:${HOST_PORT}:9371" \
	--volume "${ZOTERO_DIR}:/library:ro" \
	--volume "${DATA_DIR}/output:/app/output" \
	${MOUNT_CONFIG[@]+"${MOUNT_CONFIG[@]}"} \
	--env SERVER_HOST=0.0.0.0 \
	--env SERVER_PORT=9371 \
	--env TZ="${TZ:-UTC}" \
	"${IMAGE}:${REMOTE_SHA}" \
	server \
	--zotero-sqlite /library/zotero.sqlite \
	${PRMA_ARGS[@]+"${PRMA_ARGS[@]}"} >/dev/null

say "started ${CONTAINER} from ${IMAGE}:${REMOTE_SHA:0:12}"

# The first boot snapshots the whole library before it listens , so give it a
# while before calling it broken.
URL="http://${BIND_ADDR}:${HOST_PORT}"
http_ok "${URL}/api/version" || [ $? -ne 2 ] || {
	say "no curl / wget on this host , so not waiting :: ${URL}/"
	say "logs :: ./dockerRun.sh --logs"
	exit 0
}

say "waiting for the server to answer ..."
WAITED=0
while [ "$WAITED" -lt 120 ] ; do
	if http_ok "${URL}/api/version" ; then
		say "up :: ${URL}/"
		say "logs :: ./dockerRun.sh --logs"
		exit 0
	fi
	if [ -z "$( docker ps -q --filter "name=^/${CONTAINER}$" )" ] ; then
		warn "container exited -- last 40 lines :"
		docker logs --tail 40 "$CONTAINER" >&2 || true
		exit 1
	fi
	sleep 2
	WAITED=$(( WAITED + 2 ))
done

warn "no answer after 120s ; it may still be building its first snapshot / index."
warn "watch it with : ./dockerRun.sh --logs"
