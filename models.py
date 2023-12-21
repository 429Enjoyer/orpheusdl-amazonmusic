import dataclasses
from datetime import datetime, timedelta
import typing
import enum
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
    web_client_config: AmazonWebConfig = None
    tier: "AmazonMusicTier" = None

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

    @staticmethod
    def is_dict_of_instance(creds_to_test: dict):
        return all(name in creds_to_test for name in AmazonMusicMobileAPICredentials.__dataclass_fields__.keys())

    @property
    def access_token_expires(self) -> timedelta:
        return self.expires - datetime.now()

    @property
    def access_token_expired(self) -> bool:
        return self.expires < datetime.now()

class AmazonMusicTier(enum.Flag):
    FREE = enum.auto()
    PRIME = enum.auto()
    UNLIMITED = enum.auto()

    @property
    def internal_content_tiers(self):
        return {
            AmazonMusicTier.FREE: ["NIGHTWING_CONTENT", "OWNED_CONTENT"],
            AmazonMusicTier.PRIME: ["ROBIN_CONTENT", "SONIC_CONTENT", "NIGHTWING_CONTENT", "OWNED_CONTENT"],
            AmazonMusicTier.UNLIMITED: ["KATANA_CONTENT", "HAWKFIRE_CONTENT", "NIGHTWING_CONTENT", "OWNED_CONTENT"]
        }[self]