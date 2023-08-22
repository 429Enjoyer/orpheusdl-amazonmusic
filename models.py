import dataclasses
from datetime import datetime, timedelta
import typing

import rsa


@dataclasses.dataclass(frozen=False)
class AmazonWebConfig:
    csrf_token: str
    csrf_ts: str
    csrf_rnd: str

    device_id: str
    device_type: str
    marketplace_id: str
    session_id: str

    music_territory: str
    """ ISO 3166-1 Alpha-2 country code """
    locale: str
    """ displayLanguage, ISO 639-1 language code (e.g. en_CA, fr_FR, ja_JP etc.) """
    region: str
    """ siteRegion, continent identifier (e.g. NA, EU, FE, etc.) """

    access_token: typing.Optional[str] = None
    customer_id: str = "A2ZYP8SXGBYADP"  # passed as "" if using cookies.txt
    user_tld: str = "com"


@dataclasses.dataclass(frozen=False, slots=True)
class AmazonMusicMobileAPICredentials:
    adp_token: str
    device_private_key: rsa.PrivateKey
    access_token: str
    refresh_token: str
    expires: datetime
    website_cookies: dict
    store_authentication_cookie: dict
    tld: str
    device_info: dict
    customer_info: dict
    customer_id: typing.Optional[str] = None
    web_client_config: typing.Optional[AmazonWebConfig] = None

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, creds_dict: dict):
        web_client_config = None
        if web_client_config_dict := creds_dict.get("web_client_config"):
            web_client_config = AmazonWebConfig(**web_client_config_dict)
            creds_dict.pop("web_client_config")

        creds_dict.update({"web_client_config": web_client_config})

        inst = cls(**creds_dict)

        return inst

    @property
    def access_token_expires(self) -> timedelta:
        return self.expires - datetime.now()

    @property
    def access_token_expired(self) -> bool:
        return self.expires < datetime.now()
