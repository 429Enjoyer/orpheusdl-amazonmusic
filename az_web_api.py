import dataclasses
import json
import logging
import math
import os
from pathlib import Path
import pprint
import random
import re
import time
import typing
import uuid
from bs4 import BeautifulSoup, PageElement, ResultSet
from audible import Authenticator
import httpx
import brotli
import urllib3
import urllib3.exceptions
import requests
from .models import AmazonWebConfig, AmazonMusicMobileAPICredentials

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOGGER = logging.getLogger("ripperino-amazon")
# LOGGER = logging.getLogger(__name__)
# this module is for the amazon music api
    

class AmazonWebAPI:
    def __init__(self, mobile_app_credentials: AmazonMusicMobileAPICredentials, mobile_app_cookies: typing.Optional[dict]):
        self.user_agent = "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/112.0"

        self.cookie_path = Path("config/cookies.txt")
        cookies = {}
        if self.cookie_path.exists():
            cookies = self.get_cookie_from_file(self.cookie_path)
        else:
            LOGGER.warning("Could not find cookies.txt, skipping..")

        self.music_url = "https://music.amazon."

        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "content-type": "application/json",
            "User-Agent": self.user_agent,
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            'origin': f"{self.music_url}{AmazonWebConfig.user_tld}"
        }

        self.session = httpx.Client(headers=headers, cookies=cookies | mobile_app_credentials.website_cookies | dict(mobile_app_cookies))

        self.config_loaded = False
        self.credentials: AmazonWebConfig | None = None
        self.mobile_app_credentials = mobile_app_credentials
        return

    def get(self, url):
        return self.session.get(url)

    def post(self, url, data, headers: dict | None):
        if not headers:
            headers = {}
        return self.session.post(url, data=data, headers=headers) #dict(self.session.headers) | 

    def get_cookie_from_file(self, path: Path):
        cookies = {}
        with open(path, 'r') as f:
            for l in f:
                if not re.match(r"^#", l) and not re.match(r"^\n", l):
                    line_fields = l.strip().replace('&quot;', '"').split('\t')
                    cookies[line_fields[5]] = line_fields[6]
        return cookies
    
    def get_config(self, response_text: typing.Optional[str] = None, country_tld: typing.Optional[str] = None):
        """
        Get the config from the amazon music page
        
        Args:
            `response_text`: The response text if you wish to only refresh
            the configuration. Optional.
            `country_tld`: The TLD to use. By default, it is "com"
        """
        if not response_text:
            url = f"{self.music_url}{country_tld or AmazonWebConfig.user_tld}/"
            LOGGER.debug(url)
            resp = self.get(url)
            response_text = resp.text

        content = self.parse_html(response_text)

        script_list: ResultSet[PageElement] = content.find_all("script")
        for scripts in script_list:
            if not 'appConfig' in scripts.contents[0]:
                continue

            LOGGER.debug(scripts)
            sc = scripts.contents[0]
            sc = sc.replace( "window.amznMusic = " , "" )
            sc = sc.replace( "appConfig:" , "\"appConfig\":" )
            sc = sc.replace( "ssr: false," , "\"ssr\":\"\"" )
            sc = sc.replace( "false" , "\"\"" )
            sc = sc.replace( "true" , "\"\"" )
            sc = sc.replace( os.linesep , "" )
            sc = sc.replace( ";" , "" )
            # test if the user has a subscription
            # if not 'tier' in sc:
            #     print('No tier available, log-on was not successful.')
            #     break
            app_config = dict(json.loads(sc)['appConfig'])
            config = AmazonWebConfig(
                access_token=app_config['accessToken'],
                csrf_token=app_config['csrf']['token'],
                csrf_rnd=app_config['csrf']['rnd'],
                csrf_ts=app_config['csrf']['ts'],
                device_id=app_config['deviceId'],
                device_type=app_config['deviceType'],
                customer_id=app_config['customerId'] or AmazonWebConfig.customer_id,
                marketplace_id=app_config['marketplaceId'],
                session_id=app_config['sessionId'],
                music_territory=app_config['musicTerritory'],
                customer_lang=app_config['musicTerritory'].lower(),
                locale=app_config['displayLanguage'],
                region=app_config['siteRegion'] or "NA",
                user_tld=country_tld or AmazonWebConfig.user_tld
            )
            LOGGER.debug('Config available')
            # LOGGER.debug(json.dumps(app_config, indent=4))
            LOGGER.debug(json.dumps(dataclasses.asdict(config), indent=4))
            self.config_loaded = True
            self.credentials = config
            return config
        
        LOGGER.error('No config available, log-on was not successful.')
        
        return None
    
    def get_license_response(self, challenge: str):
        url = f"{self.music_url}{self.credentials.user_tld}/{self.credentials.region}/api/dmls/"
        target = "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getLicenseForPlaybackV2"
        headers = self.prep_request_header(target)
        # print(url)
        
        data = {
            "appInfo": {
                "musicAgent": self.get_maestro_id()
            },
            "Authorization": f"Bearer {self.credentials.access_token or self.mobile_app_credentials.access_token}",
            "customerId": self.credentials.customer_id,
            "deviceToken": {
                "deviceId": self.credentials.device_id, #self.credentials.device_id
                "deviceTypeId": self.credentials.device_type, #self.credentials.device_type
            },
            "DrmType": "WIDEVINE", 
            "licenseChallenge": f"{challenge}"
        }
        LOGGER.debug(json.dumps(data))
        response = self.session.post(
            url=url,
            data=json.dumps(data),
            headers=headers,
        )
        
        LOGGER.debug(str(response.content))
        if response.status_code == 200:
            return str(response.json().get('license', ""))
        return
    
    def get_album_info(self, track_asin: str, album_asin: str):
        # NOTE: Dead end as of right now (July 26, 2023) (Sorry this content is not avaliable in your region)
        # NOTE: this API endpoint may not be avalible for items in which they are behind a paywall (AMU)
        headers = {
            'User-Agent': self.user_agent,
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': f'https://music.amazon.{self.credentials.user_tld}/',
            'Content-Type': 'text/plain;charset=UTF-8',
            'Host': 'na.mesk.skill.music.a2z.com',
            # 'Content-Length': '2770',
            'Origin': f'https://music.amazon.{self.credentials.user_tld}',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'cross-site',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
            # Requests doesn't support trailers
            'TE': 'trailers',
        }

        data = {
            "at":f"{track_asin}", # The Track ASIN
            "headers": json.dumps({
                "x-amzn-authentication": json.dumps({
                    "interface":"ClientAuthenticationInterface.v1_0.ClientTokenElement",
                    "accessToken": self.mobile_app_credentials.access_token
                }).replace('\\\\', '\\'), 
                "x-amzn-device-model":"WEBPLAYER",
                "x-amzn-device-width":"1920",
                "x-amzn-device-height":"1080",
                "x-amzn-device-family":"WebPlayer",
                "x-amzn-device-id":f"{self.credentials.device_id}",
                "x-amzn-user-agent":f"{self.user_agent}",
                "x-amzn-session-id":f"{self.credentials.session_id}",
                "x-amzn-request-id":f"{str(uuid.uuid4()).lower()}",
                "x-amzn-device-language":"en_US",
                "x-amzn-currency-of-preference":"USD",
                "x-amzn-os-version":"1.0",
                "x-amzn-application-version":"1.0.12528.0",
                "x-amzn-device-time-zone":"America/Toronto",
                "x-amzn-timestamp":f"{str(int(time.time()))}",
                "x-amzn-csrf":json.dumps({
                    "interface":"CSRFInterface.v1_0.CSRFHeaderElement",
                    "token":f"{self.credentials.csrf_token}",
                    "timestamp":f"{self.credentials.csrf_ts}",
                    "rndNonce":f"{self.credentials.csrf_rnd}"
                }).replace('\\\\', '\\'),
                "x-amzn-music-domain":f"music.amazon.{self.credentials.user_tld}",
                "x-amzn-referer":f"music.amazon.{self.credentials.user_tld}",
                "x-amzn-affiliate-tags":"",
                "x-amzn-ref-marker":"",
                "x-amzn-page-url":f"https://music.amazon.{self.credentials.user_tld}/albums/{album_asin}",
                "x-amzn-weblab-id-overrides":"",
                # Video player token can be omitted, they return it as the first index
                # "x-amzn-video-player-token":json.dumps({
                #     "interface":"VideoPlaybackInterface.v1_0.VideoPlaybackHeaderElement",
                #     "token":"eyJhbGciOiJIUzUxMiJ9.CjRhbXpu...",
                #     "expirationMS":1690482237910
                # }).replace('\\\\', '\\'),
                "x-amzn-feature-flags":""
            }),
            "id":f"{album_asin}", # The Album ASIN
            "userHash":json.dumps({
                "level":"HD_MEMBER"
            }),
        }
        # data = f'"{data}"'
        LOGGER.debug(json.dumps(data).replace(': ', ":"))
        url = 'https://na.mesk.skill.music.a2z.com/api/playCatalogAlbum'
        opt_resp = self.session.options(
           url=url,
           headers=headers 
        )
        LOGGER.debug(opt_resp.cookies)

        response = self.session.post(
            url=url,
            headers=headers,
            json=data,
            cookies=dict(opt_resp.cookies.items()) | self.mobile_app_credentials.website_cookies
        )
        LOGGER.debug([f"{k}: {v}" for k, v in vars(response).items()])
                
        LOGGER.debug(str(response.content))
        resp_json = response.json()
        LOGGER.debug(json.dumps(resp_json, indent=2))
        return dict(resp_json)

    def prep_request_header(self, amz_target=None):
        """
        Request header preparation
        :param str amz_target: API endpoint
        """
        if self.config_loaded is not True:
            raise ValueError("You must login first!")

        head = {
            'Accept': '*/*', #application/json, text/javascript, 
            'Accept-Encoding': 'gzip, deflate, br', #
            'Accept-Language': f'{self.credentials.locale},en-US,en;q=0.5',
            'Connection': "keep-alive",
            'csrf-token': self.credentials.csrf_token,
            'csrf-rnd': self.credentials.csrf_rnd,
            'csrf-ts': self.credentials.csrf_ts,
            'Host': f'music.amazon.{self.credentials.user_tld}',
            'Origin': f"{self.music_url}{self.credentials.user_tld}",
            'User-Agent': self.user_agent,
            'X-Requested-With': 'XMLHttpRequest'
        }

        if amz_target is not None:
            head['Content-Encoding'] = 'amz-1.0'
            head['Content-Type'] = 'application/json'
            head['X-Amz-Target'] = amz_target
            
            head['x-amzn-RequestId'] = str(uuid.uuid4())
            head['Origin'] = f"{self.music_url}{AmazonWebConfig.user_tld}"
            head['Referer'] = f"{self.music_url}{AmazonWebConfig.user_tld}/"
            head['Authorization'] = f"Bearer {self.mobile_app_credentials.access_token}"
            head['TE'] = 'trailers'


        return head
    
    @staticmethod
    def parse_html(api_resp):
        """
        Make the request more readable
        """
        resp = re.sub(r'(?i)(<!doctype \w+).*>', r'\1>', api_resp)
        return BeautifulSoup(resp, 'html.parser')

    def get_maestro_id(self):
        """
        Calculate random Player ID
        """
        a = str(
            float.hex(
                float(
                    math.floor(16 * (1 + random.random()))
                )
            )
        )[4:5]
        uid = f'{self.ran_id()}-{self.ran_id()}-WebC-{self.ran_id()}-{self.ran_id()}{a}'
        return f'Maestro/1.0 WebCP/1.0.12528.0 ({uid})'
    
    @staticmethod
    def ran_id():
        """
        Calculate random ID
        """
        return str(
            float.hex(
                float(
                    math.floor(65536 * (1 + random.random()))
                )
            )
        )[4:8]