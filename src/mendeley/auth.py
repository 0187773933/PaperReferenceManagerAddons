#!/usr/bin/env python3

import time
import urllib.parse
import random
import requests
from playwright.sync_api import sync_playwright

from ..utils import utils

class MendeleyAuth:
	def __init__( self , args ):
		self.args = args
		self.config = utils.read_yaml( self.args.config.joinpath( "config.yaml" ) )
		self.session_path =self.args.config.joinpath( "mendeley_session.json" )

		mendeley = self.config.get( "mendeley" )
		self.client_id     = mendeley.get( "client_id" )
		self.client_secret = mendeley.get( "secret" )
		self.redirect_uri  = mendeley.get( "redirect_uri" )

		self.api_base  = "https://api.mendeley.com"
		self.auth_url  = f"{self.api_base}/oauth/authorize"
		self.token_url = f"{self.api_base}/oauth/token"
		self.headless  = False

	def load_session( self ):
		if self.session_path.exists():
			return utils.read_json( str( self.session_path ) ) or {}
		return {}

	def save_session( self , token ):
		self.session_path.parent.mkdir( parents=True , exist_ok=True )
		utils.write_json( str( self.session_path ) , token )

	def is_token_valid( self , token , skew=60 ):
		return bool( token.get( "access_token" ) and token.get( "_expires_at" , 0 ) - time.time() > skew )

	def exchange_token( self , payload ):
		payload[ "client_id" ]     = self.client_id
		payload[ "client_secret" ] = self.client_secret
		payload[ "redirect_uri" ]  = self.redirect_uri
		r = requests.post( self.token_url , data=payload , timeout=30 )
		r.raise_for_status()
		token = r.json()
		token[ "_expires_at" ] = time.time() + token.get( "expires_in" , 3600 ) - 60
		return token

	def try_refresh( self , sess ):
		refresh_token = sess.get( "refresh_token" )
		if not refresh_token:
			print( "No refresh_token available." )
			return None
		try:
			print( "Attempting refresh_token grant..." )
			token = self.exchange_token({
				"grant_type": "refresh_token" ,
				"refresh_token": refresh_token ,
			})
			# Mendeley may omit a new refresh_token; keep the old one
			token.setdefault( "refresh_token" , refresh_token )
			print( "Refresh successful." )
			return token
		except requests.HTTPError as e:
			print( f"Refresh failed: {e}" )
			return None

	def browser_login_and_capture( self ):
		"""Drive the OAuth consent screen, capture the redirect ?code=."""
		print( "Launching browser for Mendeley authorization..." )
		auth_link = self.auth_url + "?" + urllib.parse.urlencode({
			"client_id": self.client_id ,
			"redirect_uri": self.redirect_uri ,
			"response_type": "code" ,
			"scope": "all" ,
		})

		mendeley = self.config.get( "mendeley" )
		email    = mendeley.get( "email" )
		password = mendeley.get( "password" )

		code_holder = { "code": None }

		def _pause( lo , hi ):
			page.wait_for_timeout( random.uniform( lo , hi ) * 1000 )

		def _type( selector , text ):
			page.click( selector )
			_pause( 0.2 , 0.7 )
			for ch in text:
				page.type( selector , ch , delay=random.uniform( 60 , 180 ) )
				if random.random() < 0.08:          # occasional longer hitch
					_pause( 0.3 , 0.9 )

		with sync_playwright() as p:
			browser = p.chromium.launch(
				headless=self.headless ,
				args=[ "--start-maximized" ] ,
			)
			context = browser.new_context( no_viewport=True )
			page = context.new_page()

			def on_request( req ):
				if code_holder["code"]:
					return
				if req.url.startswith( self.redirect_uri ):
					qs = urllib.parse.urlparse( req.url ).query
					code_holder[ "code" ] = urllib.parse.parse_qs( qs ).get( "code" , [ None ] )[ 0 ]
					if code_holder[ "code" ]:
						print( "Captured auth code from redirect." )

			page.on( "request" , on_request )
			page.goto( auth_link , wait_until="domcontentloaded" )

			if email and password:
				try:
					print( "Autofilling email..." )
					page.wait_for_selector( "#bdd-email" , timeout=15000 )
					_pause( 0.8 , 2.2 )                       # look at the page first
					_type( "#bdd-email" , email )
					_pause( 0.4 , 1.3 )                       # before clicking Continue
					page.click( "#bdd-elsPrimaryBtn[value='emailContinue']" )

					print( "Autofilling password..." )
					page.wait_for_selector( "#bdd-password" , timeout=15000 )
					_pause( 0.7 , 1.8 )
					_type( "#bdd-password" , password )
					_pause( 0.4 , 1.3 )                       # before clicking Sign in
					page.click( "#bdd-elsPrimaryBtn[value='signin']" )
					print( "Submitted credentials." )
				except Exception as e:
					print( f"Autologin failed ({e}) -> finish manually in the browser" )
			else:
				print( "No saved credentials -> log in manually in the browser" )

			for _ in range( 300 ):
				if code_holder[ "code" ]:
					break
				page.wait_for_timeout( 1000 )

			browser.close()

		if not code_holder["code"]:
			print( "Browser login timeout (no code captured)." )
			return None

		token = self.exchange_token({
			"grant_type": "authorization_code" ,
			"code": code_holder["code"] ,
		})
		return token

	def get_access_token( self ):
		sess = self.load_session()

		if self.is_token_valid( sess ):
			print( "Using cached valid token." )
			return sess["access_token"]

		if sess:
			print( "Token expired -> trying refresh..." )
			refreshed = self.try_refresh( sess )
			if refreshed:
				self.save_session( refreshed )
				return refreshed[ "access_token" ]
			print( "Refresh failed -> falling back to browser login" )
		else:
			print( "No session found -> browser login required" )

		tok = self.browser_login_and_capture()
		if tok:
			self.save_session( tok )
			return tok[ "access_token" ]

		raise RuntimeError( "Failed to obtain Mendeley token." )