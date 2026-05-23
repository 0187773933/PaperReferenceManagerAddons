def main( args ):
	from . import main
	main.main( args )

def mendeley_download( args ):
	from ..mendeley.mendeley import Mendeley
	m = Mendeley( args )
	m.download()