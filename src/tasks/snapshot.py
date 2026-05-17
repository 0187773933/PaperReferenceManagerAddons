def get_common( args ):
	if not args.manager:
		if args.mendeley:
			args.manager = "mendeley"
		elif args.zotero:
			args.manager = "zotero"
		else:
			print( "no manager set" )
			return False
	manager_name = args.manager.lower()
	if manager_name == "mendeley":
		from ..mendeley.mendeley import Mendeley
		m = Mendeley( args )
		return m.snapshot()
	elif manager_name == "zotero":
		from ..zotero.zotero import Zotero
		z = Zotero( args )
		return z.snapshot()
	elif manager_name == "endnote":
		print( "get_snapshot_common( EndNote ) , Todo" )
		return False
	elif manager_name == "paperpile":
		print( "get_snapshot_common( Paperpile ) , Todo" )
		return False
	elif manager_name == "refworks":
		print( "get_snapshot_common( RefWorks ) , Todo" )
		return False
	elif manager_name == "readcube":
		print( "get_snapshot_common( ReadCube ) , Todo" )
		return False