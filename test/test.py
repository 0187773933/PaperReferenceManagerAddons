import glob, os
f = glob.glob(os.path.expanduser("~/Library/Application Support/Mendeley Desktop/*@www.mendeley.com.sqlite"))[0]
print(repr(f))
print(os.path.getsize(f), "bytes")
with open(f, "rb") as fh:
	print(fh.read(16))