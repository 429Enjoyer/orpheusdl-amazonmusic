import base64
import dataclasses
from enum import Enum, auto
import json
import logging
import logging.handlers
import math
import re
import secrets
import typing
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode
from xml.etree import ElementTree

import httpx
import rsa
import rsa.pkcs1
import xmltodict

# from audible import Authenticator, Client, localization
# from audible.auth import sign_request
from audible.login import (
    build_device_serial,
    check_for_approval_alert,
    check_for_captcha,
    check_for_choice_mfa,
    check_for_cvf,
    check_for_mfa,
    create_code_verifier,
    create_s256_code_challenge,
    default_approval_alert_callback,
    default_captcha_callback,
    default_login_url_callback,
    default_cvf_callback,
    default_otp_callback,
    extract_captcha_url,
    extract_code_from_url,
    get_inputs_from_soup,
    get_next_action_from_soup,
    get_soup
)
from audible.metadata import encrypt_metadata
from bs4 import BeautifulSoup
from Crypto.PublicKey import RSA
from .models import AmazonMusicMobileAPICredentials, AmazonWebConfig

LOGGER = logging.getLogger(__name__)

class APIError(Exception):
    def __init__(self, type, msg, payload):
        self.type = type
        self.msg = msg
        self.payload = payload

    def __str__(self):
        return ", ".join((self.type, self.msg, str(self.payload)))
    
class AmazonMobileApplication(Enum):
    MUSIC = auto()
    PRIME_VIDEO = auto()
    SHOPPING = auto()
    
    @property
    def device_type(self):
        return {
            self.MUSIC: "A1DL2DVDQVK3Q",
            self.PRIME_VIDEO: "A43PXU4ZN2AL1",
            self.SHOPPING: "A1MPSLFC7L5AFK",
        }[self]
        
    @property
    def assoc_handle(self):
        return {
            self.MUSIC: "amzn_tiburon_na",
            self.PRIME_VIDEO: "amzn_piv_android_v2_us",
        }[self]
        
    @property
    def official_name(self):
        return {
            self.MUSIC: "Amazon Music",
            self.PRIME_VIDEO: "Amazon Prime Video",
            self.SHOPPING: "Amazon Shopping",
        }[self]
    

class AmazonMusicMobileAPI:
    """Amazon Music API"""

    # device_type = "A1DL2DVDQVK3Q"  ##A1MPSLFC7L5AFK is for Amazon, A1DL2DVDQVK3Q is for Amazon Music and A43PXU4ZN2AL1 is for Prime Video
    application_version = "22.15.12"
    harley_version = "3.12.3.86"

    HARLEY_USER_AGENT = f"Harley/{harley_version} {AmazonMobileApplication.MUSIC.device_type}/{application_version}"
    """ Used for accessing playing DRM protected content """
    APP_USER_AGENT = f"MusicAndroid/{application_version}"
    """ Used for API requests """

    USER_AGENT = "Mozilla/5.0 (Linux; Android 11; Pixel 5 Build/RD2A.211001.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/108.0.5359.128 Mobile Safari/537.36"
    """ Used for Amazon login & other general requests """

    credentials: AmazonMusicMobileAPICredentials | None

    def __init__(
        self,
        country_code: str,
        credentials: Optional[AmazonMusicMobileAPICredentials] = None,
    ) -> None:
        default_headers = {
            "User-Agent": self.USER_AGENT,
            "Accept-Language": "en-US",
            "Accept-Encoding": "gzip",
            "x-requested-with": "com.amazon.mp3",
        }

        self.session = httpx.Client(
            # base_url=base_url,
            headers=default_headers,
            # cookies=init_cookies,
            follow_redirects=True,
        )
        if credentials:
            self.credentials = credentials
            self.session.cookies.update(credentials.website_cookies)
        else:
            self.credentials = None
            
        if len(country_code) != 2:
            raise ValueError(f"Country code must be a ISO 3166-1 alpha-2 value!, got: {country_code}")
        self.country_code = country_code
        
        return

    def login_via_mobile(
        self,
        email: str,
        password: str,
        domain: str = "com",
        country_code: str = "US",
        serial: Optional[str] = None,
        load_credentials: Optional[bool] = True,
        application: Optional[AmazonMobileApplication] = None
    ):

        if self.credentials:
            LOGGER.info("Already logged in")
            return
        if country_code == "JP":
            # Login to Prime Video first, because amazon.
            self.login_via_mobile(email=email, password=password, load_credentials=False, application=AmazonMobileApplication.PRIME_VIDEO)
            
        application = application or AmazonMobileApplication.MUSIC

        base_url = f"https://amazon.{domain}"
        init_cookies = self._build_init_cookies()

        self.session.base_url = base_url
        self.session.cookies.update(init_cookies)

        code_verifier = create_code_verifier()

        oauth_url, serial = self._build_oauth_url(
            domain="com",
            market_place_id=self.get_marketplace_id(country_code),
            code_verifier=code_verifier,
            application=application,
            serial=serial,
            region="na",
        )

        # authorization_code = self._internal_login(self, oauth_url, email, password)
        authorization_code = self._exteral_login(oauth_url)
        
        LOGGER.debug(f"Login confirmed for {email} on {application.official_name}")

        items = {
            "authorization_code": authorization_code,
            "code_verifier": code_verifier,
            "domain": domain,
            "serial": serial,
        }
        
        if not load_credentials:
            return True
        
        self.credentials = self.register(application=application, **items)

        # check home data, not required
        # TODO: move to seperate function
        # customer_home_resp = self.session.post(
        #     url=f"https://music.amazon.{self.credentials.tld}/{self.credentials.customer_info['home_region']}/api/stratus/",
        #     data={
        #         "customerId": None,  # it is not set, but it is required
        #         "deviceId": self.credentials.device_info["device_serial_number"],
        #         "deviceType": self.credentials.device_info["device_type"],
        #         "ipAddress": None,
        #         "sessionId": None,
        #     },
        #     headers={
        #         "x-amz-target": "com.amazon.stratus.StratusServiceExternal.retrieveCustomerHome",
        #         "x-amzn-RequestId": str(uuid.uuid4()),
        #     },
        # )
        # LOGGER.debug(f"{customer_home_resp.status_code} {customer_home_resp.text}")

        # confirm the device has been successfully authorized

        # device_resp = self.session.post(url=base_post, data={
        #     "customerInfo": {
        #         "customerId": "", #the value is not set, but it is required
        #         "deviceId": serial,
        #         "deviceType": self.credentials.device_info["device_type"],
        #     },
        #     "deviceId": serial,
        #     "deviceType": self.credentials.device_info["device_type"],
        #     "targetDeviceId": serial,
        #     "targetDeviceType": self.credentials.device_info["device_type"],
        # }, headers={
        #     'x-amz-target': 'com.amazon.stratus.StratusServiceExternal.retrieveDevice',
        #     'x-amzn-RequestId': str(uuid.uuid4()),
        # })
        # LOGGER.debug(f"{device_resp.status_code} {device_resp.text}")

        # TODO add a check if too many devices are registered, and if so, notify the user and add a way to remove devices via a prompt
        # get devices
        self._list_devices()
        
        if self.credentials:
            return self.credentials

        raise Exception("Login failed. Please check the log.")
    
    def _wait_for_response(self, request: httpx.Request):
        # Sometimes we get a DNS resolve error (too many requests for manifest?), this attempts to retry 5 times 
        attempt = 0
        while attempt <= 5:
            attempt += 1
            try:
                resp = self.session.send(request)
                return resp
            except httpx.ConnectError as ce:
                LOGGER.error(ce, exc_info=1)
                continue
        return

    def post(
        self, url: str, data: dict | None, headers: Optional[dict] = None, sign: Optional[bool] = True
    ) -> httpx.Response:
        # these headers assume that the url is https://music.amazon.com/NA/api/stratus/
        # TODO have a enum representing the the api endpoints for the different headers
        headers = {
            "User-Agent": self.APP_USER_AGENT,
            "android-app-version": self.application_version,
            "content-encoding": "amz-1.0",
            "accept": "application/json",
            "accept-encoding": "gzip",
            "accept-charset": "utf-8",
            "content-type": "application/json; charset=UTF-8",
        } | (headers or {})
        request = httpx.Request(
            "POST",
            url,
            cookies=self.credentials.website_cookies
            if hasattr(self, "credentials")
            else None,
            headers=headers,
            json=data,
        )
        if sign:
            self._apply_signing_auth_flow(request)
        self._apply_cookies_auth_flow(request)
        return self._wait_for_response(request)
            

    def get(self, url: str, headers: Optional[dict] = None) -> httpx.Response:
        # simple implementation, untested atm
        d_headers = {
            "User-Agent": self.APP_USER_AGENT,
            "X-Amz-RequestId": str(uuid.uuid4()),
        }
        request = httpx.Request(
            "GET", url, cookies=self.credentials.website_cookies, headers=d_headers | headers
        )
        # self._apply_signing_auth_flow(request)
        self._apply_cookies_auth_flow(request)
        return self._wait_for_response(request)
    
    def get_root(self):
        """
        Get the response of the root URL of Amazon Music.
        
        Useful for parsing the web app configuration.
        """
        return self.get(
            url=f"https://music.amazon.{self.credentials.tld}/",
            headers={"User-Agent": self.USER_AGENT}
        ).text

    def get_metadata(
        self, asins: str | typing.Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Get metadata for a track, album, playlist or artist.


        Track ASIN -> `response.json()['tracksList'][0]`

        Album ASIN -> `response.json()['albumsList'][0]`

        Artist ASIN -> `response.json()['artistList'][0]`
        
        ## List of avaliable features:
        [fullAlbumDetails, playlistLibraryAvailability, disableSubstitution, childParentOwnership, trackLibraryAvailability,
        hasLyrics, ownership, expandTracklist, includeVideo, requestAudioVideo, popularity, albumArtist, collectionLibraryAvailability,
        includePurchaseDetails, editorialAssociations]
        """
        #TODO figure out how to get avaliable qualities through this api (see mobile and web requests to get what i mean)
        # Valid keywords to Amazon JP (unknown)
        # objectId,fileName,fileExtension,fileSize,creationDate,lastUpdatedDate,orderId,asin,purchaseDate,localFilePath,md5,status,purchased,uploaded,title,sortTitle,rating,marketplace,physicalOrderId,assetType,artistName,artistAsin,contributors,trackNum,discNum,primaryGenre,duration,bitrate,composer,songWriter,performer,lyricist,publisher,errorCode,instantImport,primeStatus,isMusicSubscription,albumName,albumAsin,albumArtistName,albumArtistAsin,albumContributors,albumRating,albumPrimaryGenre,albumReleaseDate,sortArtistName,sortAlbumName,sortAlbumArtistName,audioUpgradeDate,parentalControls,assetEligibility,eligibility,internalTags
        asins = [asins] if isinstance(asins, str) else list(asins)
        response = self.post(
            url=f"https://music.amazon.{self.credentials.tld}/{self.credentials.customer_info['home_region']}/api/muse/",
            headers={
                "User-Agent": self.APP_USER_AGENT,
                "x-amz-target": "com.amazon.musicensembleservice.MusicEnsembleService.lookup",
                "X-Amz-Requestid": str(uuid.uuid4()),
            },
            data={
                "allowedParentalControls": {"hasExplicitLanguage": True},
                "asins": asins,
                "currencyOfPreference": None,
                "customerIP": None,
                "customerId": None,
                "deviceId": self.credentials.device_info["device_serial_number"],
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
                "features": [
                    "ownership",
                    "expandTracklist",
                    "hasLyrics",
                    "includeVideo",
                    "requestAudioVideo",
                    "popularity",
                    "expandTracklist",
                    "fullAlbumDetails",
                    "includePurchaseDetails",
                    "editorialAssociations",
                    "trackLibraryAvailability",
                    "collectionLibraryAvailability",
                    "playlistLibraryAvailability"
                ],
                "filters": None,
                "lang": "en_US",  # en_US i wonder if ja_JP would work for japanese | No it doesn't
                "marketplaceId": None,
                "metadataLang": None,
                "musicRequestIdentityContextToken": None,
                "musicTerritory": self.country_code,
                "requestedContent": "ALL_STREAMABLE",
                "sessionId": None,
                "stub": None,
            },
        )
        if response.status_code != 200:
            raise Exception(
                f"Failed to get track manifest: {response.status_code} {response.text}"
            )
        resp_json = response.json()
        LOGGER.debug(json.dumps(resp_json, indent=2))
        return resp_json

    def get_track_lyrics(
        self, track_asin: str
    ) -> dict[str, Any]:
        """
        Get the lyrics for a track.

        Response format:

        A dict with the following keys:

        `lrcSource`: Unknown representation. Usually 'AMAZON_INTERNAL'.

        `lyrics`: A dictionary with the following keys:
            `explicitLyricsStatus`: A string with the value 'unfilteredLyrics'. (Other values unknown)

            `lines`: A list of dictionaries with the following keys:
                `endTime`: The end time of the lyric in milliseconds.
                `startTime`: The start time of the lyric in milliseconds.
                `text`: The lyric text.

            `writers`: A list of strings with the lyric writers.

        `lyricsResponseCode`: A string with the value '1002' if the lyrics were found, '2001' if not.

        `lyricsSource`: The source of the lyrics. One version is 'MUSIX_MATCH'.

        `trackAsinAndMarketplace`: A dictionary with the following keys:
            `asin`: The track asin.
            `marketplaceId`: The ID of the marketplace.
        """

        response = self.post(
            url=f"https://music-xray-service.amazon.{self.credentials.tld}/",
            headers={
                "User-Agent": self.APP_USER_AGENT,
                "x-amz-target": "com.amazon.musicxray.MusicXrayService.getLyricsByTrackAsinBatch",
                "X-Amz-Requestid": str(uuid.uuid4()),
            },
            data={
                "trackAsinsAndMarketplaceList": [
                    {
                        "asin": track_asin,
                        "musicTerritory": self.country_code,
                    }
                ]
            },
        )

        return dict(response.json()["lyricsResponseList"][0])

    def get_track_info(self, track_asin: str):
        resp = self.get_metadata(track_asin)['trackList']
        if len(resp) > 1:
            raise Exception("Failed to get track manifest: tracklist is greater than 1")
        return resp[0]

    def get_track_manifest(self, asin: str):
        """
        Get the playback manifest of a track (MPD)

        Returns:
        The Amazon Music Dash Manifest as a `xml.etree.ElementTree`
        
        TRACK_PSSH + SIREN_KATANA = All audio format (Lossless and 360) but, ffmpeg can convert (lossless) but doesn't play on DeaDBeeF?
        TRACK_PSSH + SIREN_KATANA_NO_CLEAR_LEAD = No issues, only up to lossless
        """
        music_agent = f"Harley/{self.harley_version} Harley/{self.application_version} ( {str(uuid.uuid4())} {asin} )"
        response = self.post(
            url=f"https://music.amazon.{self.credentials.tld}/{self.credentials.customer_info['home_region']}/api/dmls/getDashManifestsV2",
            headers={
                "User-Agent": self.HARLEY_USER_AGENT,
                "X-Amz-Requestid": str(uuid.uuid4()),
                "X-Amz-Target": "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getDashManifestsV2",
                "Accept": "application/json, text/javascript, */*",
            },
            data={
                "appInfo": {"musicAgent": music_agent},
                "contentIdList": [
                    {
                        "identifier": asin,
                        "identifierType": "ASIN",
                    }
                ],
                "contentProtectionList": [
                    # "GROUP_PSSH", # for entitlement key
                    "TRACK_PSSH",  # not used in the amazon app, but is valid # web playback uses TRACK_PSSH
                ],
                "customerInfo": {
                    # "entitlementList": [
                    #     "HAWKFIRE",
                    #     "KATANA",
                    # ],
                    "marketplaceId": self.get_marketplace_id(self.country_code),
                    "territoryId": self.country_code,
                },
                "customerId": self.credentials.customer_id,
                "deviceToken": {
                    "deviceId": self.credentials.device_info["device_serial_number"],
                    "deviceTypeId": AmazonMobileApplication.MUSIC.device_type,
                },
                "musicDashVersionList": [
                    # "SIREN", #untested
                    "SIREN_KATANA", #with 360 audio, but keeps on getting invalid key size (with group_pssh)? PSSH Entitled key size is always 32 bytes, not sure why (brings error if used)
                    # "SIREN_KATANA_NO_CLEAR_LEAD", #this and no entitlement, is what is used by Amazon Music Web
                ],
                # "try3dAsinSubstitution": True,
                "tryAsinSubstitution": True,
            },
        )
        resp_dict = response.json()
        # print(resp_dict)
        if (
            response.status_code != 200
            or resp_dict["contentResponseList"][0]["contentResponseStatusCode"]
            != "SUCCESS"
        ):
            raise Exception(
                f"Failed to get track manifest: {response.status_code} {response.text}"
            )

        # return xmltodict.parse(resp_dict["contentResponseList"][0]["manifest"])
        manifest = resp_dict["contentResponseList"][0]["manifest"]
        return xmltodict.parse(manifest)
        # Removes default namespace definition, don't do that!
        # manifest = re.sub(r'xmlns="[^"]+"', '', manifest, count=1)
        # return ElementTree.fromstring(manifest)
        # return resp_dict["contentResponseList"][0]["manifest"]

    def get_license_response(self, asin: str, challenge: str) -> str:
        """
        Do not use this function.
        It does not work, unless you have the right System ID required.
        """
        # NOTE don't understand why it posts twice to the same endpoint
        self.refresh_access_token(force=True)
        # print(self.credentials.access_token)
        # print(self.credentials.website_cookies)
        response = self.post(
            # url="https://music.amazon.com/NA/api/dmls/getLicenseForPlaybackV2",
            url="https://music.amazon.com/NA/api/dmls/",
            data={
                "DrmType": "WIDEVINE_ENTITLEMENT", # entitlement is not possible without the proper widevine device (9480)
                # "DrmType": "WIDEVINE", #TODO add request cookies (required for this, find out which specifically)
                "appInfo": {
                    "musicAgent": f"Harley/{self.harley_version} Harley/{self.application_version} ( {str(uuid.uuid4())} {asin} )"
                },
                "deviceToken": {
                    "deviceId": self.credentials.device_info["device_serial_number"],
                    "deviceTypeId": AmazonMobileApplication.MUSIC.device_type,
                },
                "licenseChallenge": challenge,
                "persistent": False,
            },
            headers={
                # "User-Agent": self.HARLEY_USER_AGENT,
                "User-Agent": self.USER_AGENT,
                "X-Amz-requestid": str(uuid.uuid4()),
                "X-Amz-Target": "com.amazon.digitalmusiclocator.DigitalMusicLocatorServiceExternal.getLicenseForPlaybackV2",
                "Origin": "https://music.amazon.com",
                "Referer": "https://music.amazon.com/",
            },
        )
        
        if response.status_code != 200:
            raise Exception(f"Failed to get license: {response.status_code} {response.text}")

        return response.json()['license']

    def register(
        self,
        authorization_code: str,
        code_verifier: bytes,
        domain: str,
        serial: str,
        application: AmazonMobileApplication
    ):
        """Registers a dummy Amazon device for Amazon Music.

        Args:
            authorization_code: The code given after a successful authorization
            code_verifier: The verifier code from authorization
            domain: The top level domain of the requested Amazon server (e.g. com).
            serial: The device serial

        Returns:
            Additional authentication data needed for access Amazon Music API.

        """

        if hasattr(self, "credentials") and self.credentials:
            LOGGER.warning("Already registered, returning existing credentials")
            return self.credentials

        device_name = f"ripperino {uuid.uuid1()} - Android Device (MP3)"
        LOGGER.debug(f"Registering device {device_name} with serial {serial}")

        body = {
            "requested_token_type": [
                "bearer",
                "mac_dms",
                "website_cookies",
                "store_authentication_cookie",
            ],
            "cookies": {"website_cookies": [], "domain": f".amazon.{domain}"},
            "registration_data": {
                "domain": "Device",
                "app_version": self.application_version,
                "device_serial": serial,
                "device_type": application.device_type,
                "device_name": device_name,
                "os_version": "11",
                "software_version": "522151214",
                "device_model": "Pixel 5",
                "app_name": application.official_name,
            },
            "auth_data": {
                "client_id": self._build_client_id(serial, application),
                "authorization_code": authorization_code,
                "code_verifier": code_verifier.decode(),
                "code_algorithm": "SHA-256",
                "client_domain": "DeviceLegacy",
                # "client_domain": "Device",
            },
            "requested_extensions": ["device_info", "customer_info"],
        }

        resp = httpx.post(f"https://api.amazon.{domain}/auth/register", json=body)

        LOGGER.debug(json.dumps(resp.json(), indent=4))
        resp_json = resp.json()
        if resp.status_code != 200:
            raise Exception(resp_json)

        success_response = resp_json["response"]["success"]

        tokens = dict(success_response["tokens"])
        adp_token = tokens["mac_dms"]["adp_token"]
        device_private_key = str(tokens["mac_dms"]["device_private_key"])

        pem_prefix = "-----BEGIN RSA PRIVATE KEY-----\n"
        pem_suffix = "\n-----END RSA PRIVATE KEY-----"
        if not device_private_key.startswith(
            pem_prefix
        ) and not device_private_key.endswith(pem_suffix):
            key = RSA.import_key(base64.b64decode(device_private_key))
            device_private_key = rsa.PrivateKey.load_pkcs1(key.export_key("PEM"))
            # device_private_key = key.export_key("PEM").decode('utf-8')
        else:
            key = rsa.PrivateKey.load_pkcs1(device_private_key)

        store_authentication_cookie = tokens["store_authentication_cookie"]
        access_token = tokens["bearer"]["access_token"]
        refresh_token = tokens["bearer"]["refresh_token"]
        expires_s = int(tokens["bearer"]["expires_in"])
        expires = datetime.utcnow() + timedelta(seconds=expires_s)

        extensions = success_response["extensions"]
        device_info = dict(extensions["device_info"])
        customer_info = dict(extensions["customer_info"])

        website_cookies = {
            cookie["Name"]: str(cookie["Value"]).replace(r'"', r"")
            for cookie in tokens.get("website_cookies", [{}])
        }

        # reset session cookies (init cookies)
        self.session.cookies.clear()
        # set website cookies as default cookies
        self.session.cookies.update(website_cookies)

        self.credentials = AmazonMusicMobileAPICredentials(
            adp_token=adp_token,
            device_private_key=device_private_key,
            access_token=access_token,
            refresh_token=refresh_token,
            expires=expires,
            website_cookies=website_cookies,
            store_authentication_cookie=store_authentication_cookie,
            device_info=device_info,
            customer_info=customer_info,
            tld=domain,
        )

        # authorize device for usage on Amazon Music
        auth_device_resp = dict(self._authorize_device(device_serial=serial).json())

        self.credentials.customer_id = auth_device_resp["device"]["customerId"]

        return self.credentials

    @staticmethod
    def _build_client_id(serial: str, app: Optional[AmazonMobileApplication] = None) -> str:
        if app is not None:
            device_type = app.device_type
        else:
            device_type = AmazonMobileApplication.MUSIC
        client_id = serial.encode() + f"#{device_type}".encode("utf-8")
        return client_id.hex()

    @staticmethod
    def _build_init_cookies() -> dict[str, str]:
        """Build initial cookies to prevent captcha in most cases."""

        frc = secrets.token_bytes(313)
        frc = base64.b64encode(frc).decode("ascii").rstrip("=")
        amzn_app_id = "MAPAndroidLib-1.3.4028.0"

        map_md = {
            "device_registration_data": {"software_version": "130050002"},
            "app_identifier": {
                "package": "com.amazon.mp3",
                "SHA-256": [
                    "2f19adeb284eb36f7f07786152b9a1d14b21653203ad0b04ebbf9c73ab6d7625"
                ],
                "app_version": "522151214",
                "app_version_name": AmazonMusicMobileAPI.application_version,
                "app_sms_hash": "QGCBba+brC5",
                "map_version": amzn_app_id,
            },
            "app_info": {
                "auto_pv": 0,
                "auto_pv_with_smsretriever": 0,
                "smartlock_supported": 0,
                "permission_runtime_grant": 2,
            },
            "device_user_dictionary": [],  # maybe adding the email would help bypass captcha
        }

        map_md = json.dumps(map_md)
        map_md = base64.b64encode(map_md.encode()).decode().rstrip("=")

        return {"frc": frc, "map-md": map_md, "amzn-app-id": amzn_app_id}

    @staticmethod
    def _build_oauth_url(
        domain: str,
        market_place_id: str,
        code_verifier: bytes,
        application: AmazonMobileApplication,
        region: Optional[str] = None,
        serial: Optional[str] = None,
        assoc_handle: Optional[str] = None
    ) -> tuple[str, str]:
        """Builds the url to login to Amazon Music."""

        serial = (
            serial or "PIXEL5" + build_device_serial()
        )  # requires some random model name at the start
        client_id = AmazonMusicMobileAPI._build_client_id(serial, application)
        code_challenge = create_s256_code_challenge(code_verifier)

        LOGGER.debug("device serial: %s", serial)
        LOGGER.debug("client id: %s", client_id)

        base_url = f"https://www.amazon.{domain}/ap/signin"
        return_to = f"https://www.amazon.{domain}/ap/maplanding"

        oauth_params = {
            "openid.pape.max_auth_age": "0",
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "accountStatusPolicy": "P1",
            "language": "en_US",
            "openid.return_to": return_to,
            "openid.assoc_handle": application.assoc_handle,
            "openid.oa2.response_type": "code",
            "openid.mode": "checkid_setup",
            "openid.ns.pape": "http://specs.openid.net/extensions/pape/1.0",
            "openid.oa2.code_challenge_method": "S256",
            "openid.ns.oa2": f"http://www.amazon.{domain}/ap/ext/oauth/2",
            "openid.oa2.code_challenge": code_challenge,
            "openid.oa2.scope": "device_auth_access",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.oa2.client_id": f"device:{client_id}",
            "disableLoginPrepopulate": "0",
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "forceMobileLayout": "true",  # custom, unsure if required by azm or is useless
            "marketPlaceId": market_place_id,  # custom, unsure if required by azm or is useless
        }

        return f"{base_url}?{urlencode(oauth_params)}", serial

    @staticmethod
    def _now_to_unix_ms() -> int:
        return math.floor(datetime.now().timestamp() * 1000)

    @staticmethod
    def _get_app_metadata(user_agent: str, oauth_url: str) -> str:
        """
        Returns json-formatted metadata to simulate sign-in from an Android Amazon Music app.
        """

        meta_dict = {
            "metrics": {
                "el": 0,
                "script": 0,
                "h": 1,
                "batt": 0,
                "perf": 0,
                "auto": 0,
                "tz": 0,
                "fp2": 0,
                "lsubid": 0,
                "browser": 0,
                "capabilities": 1,
                "gpu": 0,
                "dnt": 0,
                "math": 0,
                "tts": 0,
                "input": 1,
                "canvas": 0,
                "captchainput": 0,
                "pow": 0,
            },
            "start": 1672106376599,
            "interaction": {
                "clicks": 1,
                "touches": 1,
                "keyPresses": 33,
                "cuts": 0,
                "copies": 0,
                "pastes": 0,
                "keyPressTimeIntervals": [168, 343, 131, 1118, 92, 192, 205, 98, 144],
                "mouseClickPositions": ["74,294"],
                "keyCycles": [16, 10, 8, 7, 8, 13, 11, 12, 17, 12],
                "mouseCycles": [16],
                "touchCycles": [],
            },
            "scripts": {
                "dynamicUrls": [
                    "https://images-na.ssl-images-amazon.com/images/I/31YXrY93hfL.js",
                    "https://images-na.ssl-images-amazon.com/images/I/61NeHXhGwSL._RC|11Y+5x+kkTL.js,01qkmZhGmAL.js,71-8cBvmf4L.js_.js?AUIClients/MusicBlackAndBlueAndroidSkin&amp;KK9dlo3A#mobile.412402-T1.412405-T1",
                    "https://images-na.ssl-images-amazon.com/images/I/21ZMwVh4T0L._RC|21OJDARBhQL.js,218GJg15I8L.js,31lucpmF4CL.js,2119M3Ks9rL.js,51X7BnRF64L.js_.js?AUIClients/AuthenticationPortalAssets&amp;QmmAyoMU#mobile.194821-T1",
                    "https://images-na.ssl-images-amazon.com/images/I/01wGDSlxwdL.js?AUIClients/AuthenticationPortalInlineAssets",
                    "https://images-na.ssl-images-amazon.com/images/I/41XHAz6BnWL.js?AUIClients/CVFAssets#mobile",
                    "https://images-na.ssl-images-amazon.com/images/I/818jIy8T6BL.js?AUIClients/SiegeClientSideEncryptionAUI",
                    "https://images-na.ssl-images-amazon.com/images/I/31IwoCo8XiL.js?AUIClients/AmazonUIFormControlsJS#mobile",
                    "https://images-na.ssl-images-amazon.com/images/I/819PzLyzJVL.js?AUIClients/FWCIMAssets",
                    "https://images-na.ssl-images-amazon.com/images/I/7195RJQQs1L.js?AUIClients/ACICAssets",
                    "https://static.siege-amazon.com/prod/profiles/AuthenticationPortalSigninNA.js",
                ],
                "inlineHashes": [
                    -1746719145,
                    776692753,
                    -1106742843,
                    -314038750,
                    172381973,
                    1292021430,
                    452512068,
                    928554431,
                    318224283,
                    -24495950,
                    1506353394,
                    700743993,
                    4606827,
                    -1611905557,
                    1800521327,
                    2118020403,
                    1532181211,
                    1502018687,
                    841624991,
                    -1677151674,
                ],
                "elapsed": 28,
                "dynamicUrlCount": 10,
                "inlineHashesCount": 20,
            },
            "history": {"length": 2},
            "battery": {},
            "performance": {
                "timing": {
                    "navigationStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "unloadEventStart": 0,
                    "unloadEventEnd": 0,
                    "redirectStart": 0,
                    "redirectEnd": 0,
                    "fetchStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "domainLookupStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "domainLookupEnd": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "connectStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "connectEnd": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "secureConnectionStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "requestStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "responseStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "responseEnd": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "domLoading": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "domInteractive": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "domContentLoadedEventStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "domContentLoadedEventEnd": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "domComplete": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "loadEventStart": AmazonMusicMobileAPI._now_to_unix_ms(),
                    "loadEventEnd": AmazonMusicMobileAPI._now_to_unix_ms(),
                }
            },
            "automation": {
                "wd": {"properties": {"document": [], "window": [], "navigator": []}},
                "phantom": {"properties": {"window": []}},
            },
            "end": 1672106405750,
            "timeZone": -5,
            "flashVersion": None,
            "plugins": "unknown||412-732-732-24-*-*-*",
            "dupedPlugins": "unknown||412-732-732-24-*-*-*",
            "screenInfo": "412-732-732-24-*-*-*",
            "userAgent": AmazonMusicMobileAPI.USER_AGENT,
            "webDriver": False,
            "capabilities": {
                "css": {
                    "textShadow": 1,
                    "WebkitTextStroke": 1,
                    "boxShadow": 1,
                    "borderRadius": 1,
                    "borderImage": 1,
                    "opacity": 1,
                    "transform": 1,
                    "transition": 1,
                },
                "js": {
                    "audio": True,
                    "geolocation": True,
                    "localStorage": "supported",
                    "touch": True,
                    "video": True,
                    "webWorker": True,
                },
                "elapsed": 2,
            },
            "gpu": {
                "vendor": "ARM",
                "model": "Mali-T880",
                "extensions": [
                    "ANGLE_instanced_arrays",
                    "EXT_blend_minmax",
                    "EXT_float_blend",
                    "EXT_sRGB",
                    "OES_element_index_uint",
                    "OES_fbo_render_mipmap",
                    "OES_standard_derivatives",
                    "OES_vertex_array_object",
                    "WEBGL_compressed_texture_astc",
                    "WEBGL_compressed_texture_etc",
                    "WEBGL_compressed_texture_etc1",
                    "WEBGL_debug_renderer_info",
                    "WEBGL_debug_shaders",
                    "WEBGL_depth_texture",
                    "WEBGL_lose_context",
                    "WEBGL_multi_draw",
                ],
            },
            "dnt": None,
            "math": {
                "tan": "-1.4214488238747245",
                "sin": "0.8178819121159085",
                "cos": "-0.5753861119575491",
            },
            "form": {
                "ap-credential-autofill-hint": {
                    "clicks": 0,
                    "touches": 0,
                    "keyPresses": 0,
                    "cuts": 0,
                    "copies": 0,
                    "pastes": 0,
                    "keyPressTimeIntervals": [],
                    "mouseClickPositions": [],
                    "keyCycles": [],
                    "mouseCycles": [],
                    "touchCycles": [],
                    "width": 0,
                    "height": 0,
                    "totalFocusTime": 0,
                    "prefilled": False,
                },
                "password": {
                    "clicks": 1,
                    "touches": 1,
                    "keyPresses": 69,
                    "cuts": 0,
                    "copies": 0,
                    "pastes": 0,
                    "keyPressTimeIntervals": [
                        168,
                        344,
                        131,
                        1117,
                        92,
                        193,
                        203,
                        100,
                        143,
                    ],
                    "mouseClickPositions": ["41,23.053558349609375"],
                    "keyCycles": [17, 11, 8, 8, 9, 14, 11, 14, 17, 13],
                    "mouseCycles": [16],
                    "touchCycles": [],
                    "width": 346.0000305175781,
                    "height": 43.000003814697266,
                    "totalFocusTime": 0,
                    "prefilled": False,
                },
            },
            "canvas": 0,
            "token": {"isCompatible": True, "pageHasCaptcha": 0},
            "auth": {"form": {"method": "post"}},
            "errors": [],
            "version": "4.0.0",
        }
        return json.dumps(meta_dict, separators=(",", ":"))

    def _apply_signing_auth_flow(self, request: httpx.Request) -> None:
        # headers = sign_request(
        #     method=request.method,
        #     path=request.url.raw_path.decode(),
        #     body=request.content,
        #     adp_token=self.credentials.adp_token,
        #     private_key=self.credentials.device_private_key
        # )

        date = datetime.utcnow().isoformat("T") + "Z"
        body = request.content.decode("utf-8")

        data = f"{request.method}\n{request.url.raw_path.decode()}\n{date}\n{body}\n{self.credentials.adp_token}"

        key = self.credentials.device_private_key

        cipher = rsa.pkcs1.sign(data.encode(), key, "SHA-256")
        signed_encoded = base64.b64encode(cipher)

        signature = f"{signed_encoded.decode()}:{date}"

        headers = {
            "x-adp-token": self.credentials.adp_token,
            "x-adp-alg": "SHA256withRSA:1.0",
            "x-adp-signature": signature,
        }

        # LOGGER.debug(headers)

        request.headers.update(headers)
        LOGGER.info("signing auth flow applied to request")

    def _apply_cookies_auth_flow(self, request: httpx.Request) -> None:
        if not self.credentials:
            raise ValueError("You must login first!")
        cookies = {
            name: value for (name, value) in self.credentials.website_cookies.items()
        }

        httpx.Cookies(cookies).set_cookie_header(request)
        LOGGER.info("cookies auth flow applied to request")

    def _list_devices(self):
        devices_resp = self.post(
            url=f"https://music.amazon.{self.credentials.tld}/{self.credentials.customer_info['home_region']}/api/stratus/",
            data={
                "customerId": None,
                "deviceId": self.credentials.device_info["device_serial_number"],
                "deviceType": self.credentials.device_info["device_type"],
            },
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.listDevicesByCustomerId",
                "x-amzn-requestid": str(uuid.uuid4()),
            },
        )
        LOGGER.debug(
            f"{devices_resp.status_code} {json.dumps(devices_resp.json(), indent=4)}"
        )
        return devices_resp

    def _authorize_device(
        self,
        device_serial: Optional[str] = None,
        device_type: Optional[str] = None,
        home_region: Optional[str] = None,
        domain: Optional[str] = None,
    ):
        if not device_type:
            device_type = AmazonMobileApplication.MUSIC.device_type

        if not device_serial:
            device_serial = self.credentials.device_info["device_serial_number"]

        if not home_region:
            home_region = self.credentials.customer_info["home_region"]

        if not domain:
            domain = self.credentials.tld

        if not hasattr(self, "credentials") and None in (
            device_serial,
            device_type,
            home_region,
            domain,
        ):
            raise Exception("No credentials available")

        auth_device_resp = self.post(
            url=f"https://music.amazon.{domain}/{home_region}/api/stratus/",
            data={
                "capabilities": [
                    "RETRIEVE_OWNED_CONTENT",
                    "RETRIEVE_ROBIN_CONTENT",
                ],
                "customerInfo": {
                    "customerId": "",  # it is not set, but it is required
                    "deviceId": device_serial,
                    "deviceType": device_type,
                },
                "deviceId": device_serial,
                "deviceType": device_type,
                "targetDeviceId": device_serial,
                "targetDeviceType": device_type,
            },
            headers={
                "x-amz-target": "com.amazon.stratus.StratusServiceExternal.authorizeDevice",
                "x-amzn-RequestId": str(uuid.uuid4()),
            },
        )
        auth_device_resp_json = auth_device_resp.json()
        LOGGER.debug(
            f"{auth_device_resp.status_code} {json.dumps(auth_device_resp_json, indent=4)}"
        )
        return auth_device_resp
    
    def _retrieve_capability(self):
        response = self.post(
            url=f"https://music.amazon.{self.credentials.tld}/{self.credentials.customer_info['home_region']}/api/stratus/",
            headers={
                'x-amz-target': 'com.amazon.stratus.StratusServiceExternal.retrieveCapability',
                'x-amzn-requestid': str(uuid.uuid4())
            },
            data={
                "capabilityTypes": [
                    "RETRIEVE_ROBIN_CONTENT"    
                ],
                "customerId": None,
                "deviceId": self.credentials.device_info['device_serial_number'],
                "deviceType": AmazonMobileApplication.MUSIC.device_type,
            }
        )
        resp_json = response.json()
        LOGGER.debug(f"{response.status_code} {json.dumps(resp_json, indent=4)}")
        return dict(resp_json)

    def _deauthorize_device(self, device_serial: Optional[str]):
        # remove device from authorized devices in amazon music
        return

    def refresh_access_token(self, force: bool = False) -> None:
        """
        Refresh the access token

        """
        if force or self.credentials.access_token_expired:
            if self.credentials.refresh_token is None:
                message = "No refresh token found. Can't refresh access token."
                LOGGER.critical(message)
                raise Exception(message)

            body = {
                "app_name": "Amazon Music",
                "app_version": "3.56.2",
                "source_token": self.credentials.refresh_token,
                "requested_token_type": "access_token",
                "source_token_type": "refresh_token",
            }

            resp = self.post(
                f"https://api.amazon.{self.credentials.tld}/auth/token",
                data=body,
                sign=False
            )
            resp.raise_for_status()
            resp_dict = resp.json()

            expires_in_sec = int(resp_dict["expires_in"])
            expires = datetime.utcnow() + timedelta(seconds=expires_in_sec)

            self.credentials.access_token = resp_dict["access_token"]
            self.credentials.expires = expires

        else:
            LOGGER.info(
                "Access Token not expired. No refresh necessary. "
                "To force refresh please use force=True"
            )

    def _exteral_login(self, oauth_url: str):
        response_url = httpx.URL(default_login_url_callback(oauth_url))
        parsed_url = parse_qs(response_url.query.decode())

        authorization_code = parsed_url["openid.oa2.authorization_code"][0]
        return authorization_code

    def _internal_login(self, oauth_url: str, email: str, password: str):
        oauth_resp = self.session.get(oauth_url)
        LOGGER.debug(oauth_resp)
        oauth_soup = get_soup(oauth_resp)

        login_inputs = get_inputs_from_soup(oauth_soup)
        login_inputs["email"] = email
        login_inputs["password"] = password
        metadata = self._get_app_metadata(
            user_agent=self.USER_AGENT, oauth_url=oauth_url
        )
        login_inputs["metadata1"] = encrypt_metadata(metadata)
        method, url = get_next_action_from_soup(oauth_soup, {"name": "signIn"})

        login_resp = self.session.request(method, url, data=login_inputs)
        login_soup = get_soup(login_resp)

        # check for captcha
        while check_for_captcha(login_soup):
            captcha_url = extract_captcha_url(login_soup)
            if not captcha_url:
                continue
            guess = default_captcha_callback(captcha_url)

            inputs = get_inputs_from_soup(login_soup)
            inputs["guess"] = guess
            inputs["use_image_captcha"] = "true"
            inputs["use_audio_captcha"] = "false"
            inputs["showPasswordChecked"] = "false"
            inputs["email"] = email
            inputs["password"] = password

            method, url = get_next_action_from_soup(login_soup, {"name": "signIn"})

            login_resp = self.session.request(method, url, data=inputs)
            login_soup = get_soup(login_resp)

        # check for choice mfa
        # https://www.amazon.de/ap/mfa/new-otp
        while check_for_choice_mfa(login_soup):
            inputs = get_inputs_from_soup(login_soup)
            for node in login_soup.select("div[data-a-input-name=otpDeviceContext]"):
                # auth-TOTP, auth-SMS, auth-VOICE
                if "auth-TOTP" in node["class"]:
                    inp_node = node.find("input")
                    inputs[inp_node["name"]] = inp_node["value"]

            method, url = get_next_action_from_soup(login_soup)

            login_resp = self.session.request(method, url, data=inputs)
            login_soup = get_soup(login_resp)

        # check for mfa (otp_code)
        while check_for_mfa(login_soup):
            otp_code = default_otp_callback()

            inputs = get_inputs_from_soup(login_soup)
            inputs["otpCode"] = otp_code
            inputs["mfaSubmit"] = "Submit"
            inputs["rememberDevice"] = "false"

            method, url = get_next_action_from_soup(login_soup)

            login_resp = self.session.request(method, url, data=inputs)
            login_soup = get_soup(login_resp)

        # check for cvf
        while check_for_cvf(login_soup):
            print("Check your email or SMS for a code from Amazon and enter it in the below prompt.")
            cvf_code = default_cvf_callback()

            inputs = get_inputs_from_soup(login_soup)

            method, url = get_next_action_from_soup(login_soup)

            login_resp = self.session.request(method, url, data=inputs)
            LOGGER.debug("cvf resp: %s, %s", login_resp, login_resp.text)
            login_soup = get_soup(login_resp)

            inputs = get_inputs_from_soup(login_soup)
            inputs["action"] = "code"
            inputs["code"] = cvf_code

            method, url = get_next_action_from_soup(login_soup)

            login_resp = self.session.request(method, url, data=inputs)
            login_soup = get_soup(login_resp)

        # check for approval alert
        while check_for_approval_alert(login_soup):
            default_approval_alert_callback()

            # url = login_soup.find(id="resend-approval-link")["href"]
            url = login_resp.url

            login_resp = self.session.get(url)
            login_soup = get_soup(login_resp)

            while login_soup.find(
                "span", {"class": "transaction-approval-word-break"}
            ):  # a-size-base-plus transaction-approval-word-break a-text-bold
                login_resp = self.session.get(url)
                login_soup = get_soup(login_resp)
                LOGGER.info("still waiting for redirect")

        # print(login_resp.url)
        if b"openid.oa2.authorization_code" not in login_resp.url.query:
            raise Exception("Login failed. Please check the log.")

        authorization_code = extract_code_from_url(login_resp.url)
        LOGGER.debug(parse_qs(login_resp.url.query.decode()))
        return authorization_code    

    def get_marketplace_id(self, country_code: str) -> str:
        """Returns the marketplace id for a given country code"""
        # this is retrieved from the Amazon Music android app
        # marketplace ID for amazon prime video: ART4WZ8MWBX2Y
        return {
            "US": "ATVPDKIKX0DER",
            "JP": "A1VC38T7YXB528"
        }[country_code.upper()]
