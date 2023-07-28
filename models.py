import dataclasses
from datetime import datetime, timedelta
import typing

import rsa

@dataclasses.dataclass(frozen=False)
class AmazonWebConfig:
    access_token: str
    csrf_token: str
    csrf_ts: str
    csrf_rnd: str
    
    device_id: str
    device_type: str
    marketplace_id: str
    session_id: str
    
    music_territory: str # iso 3166-1 alpha-2 country code
    locale: str # displayLanguage, iso 639-1 language code (e.g. en_CA, fr_FR, etc.)
    customer_lang: str # music_territory.lower()
    region: str # siteRegion, continent identifier (e.g. NA, EU, AS, etc.)
    
    customer_id: str = "A2ZYP8SXGBYADP" # passed as "" if using cookies.txt
    user_tld: str = "com"

@dataclasses.dataclass(frozen=False)
class AmazonMusicMobileAPICredentials:
    adp_token: str
    device_private_key: rsa.PrivateKey
    access_token: str
    refresh_token: str
    expires: datetime
    website_cookies: dict
    store_authentication_cookie: dict
    domain: str
    device_info: dict
    customer_info: dict
    customer_id: typing.Optional[str] = None

    def to_dict(self):
        return dataclasses.asdict(self)

    @property
    def access_token_expires(self) -> timedelta:
        return self.expires - datetime.now()

    @property
    def access_token_expired(self) -> bool:
        return self.expires < datetime.now()