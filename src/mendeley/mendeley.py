from . import local
from .api import MendeleyAPI
from .local import MendeleyLocal

class Mendeley:
	def __init__( self , args ):
		self.args = args
		self.API = MendeleyAPI( self.args )
		self.Local = MendeleyLocal( args )

	def snapshot( self ):
		if self.args.mendeley_source == "local":
			self.snapshot = self.Local.snapshot()
		elif self.args.mendeley_source == "api":
			self.snapshot = self.API.snapshot()
		return self.snapshot