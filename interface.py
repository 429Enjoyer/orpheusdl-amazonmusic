import base64
from collections import OrderedDict
import functools
import logging
import os
import itertools
import pprint
import random
import re
import shutil
import subprocess
import sys
import typing
import dataclasses
import itertools
import httpx
from datetime import datetime, timedelta
import concurrent.futures
from urllib.parse import urlparse, parse_qs

import ffmpeg
import natsort
from pywidevine import PSSH, Cdm, Device
from pywidevine.license_protocol_pb2 import WidevinePsshData
from Crypto.Cipher import AES

from tqdm import tqdm
from xml.etree import ElementTree

from utils.models import *
from utils.utils import (
    create_temp_filename,
    download_file,
    get_clean_env,
    read_temporary_setting,
    resolve_mp4decrypt,
    resolve_shaka_packager,
    silentremove,
    sanitise_name,
)

from modules.amazonmusic.models import AmazonContinent
from .azapi import AmazonMusicMobileAPI, AmazonMusicMobileAPICredentials, AmazonMusicTier, AmazonRegion

LOGGER = logging.getLogger(__name__)

# Album/playlist search: probe a few tracks per row (not every track) for acceptable speed.
SEARCH_QUALITY_MAX_TRACK_PROBE = 3

SHAKA_PACKAGER_HELP_URL = "https://github.com/shaka-project/shaka-packager/releases/latest"


class AmazonMusicConfigError(Exception):
    """Raised when Amazon Music module settings are missing or invalid."""


def _normalize_wvd_path(wvd_path) -> str:
    if wvd_path is None:
        return ""
    return str(wvd_path).strip()


def _validate_wvd_path(wvd_path) -> str:
    path = _normalize_wvd_path(wvd_path)
    if not path:
        raise AmazonMusicConfigError(
            "Amazon Music: A Widevine device file (.wvd) is required.\n"
            "Set wvd_path in settings.json (modules → amazonmusic) or in the GUI Amazon Music tab."
        )
    if path in (".", "./", ".\\"):
        raise AmazonMusicConfigError(
            "Amazon Music: wvd_path is set to the current directory, not a .wvd file.\n"
            "Browse to your .wvd file in settings and try again."
        )
    resolved = os.path.abspath(path)
    if not os.path.isfile(resolved):
        raise AmazonMusicConfigError(
            f"Amazon Music: Widevine device file not found: {path}\n"
            "Set wvd_path to a valid .wvd file (pywidevine create-device output)."
        )
    if not resolved.lower().endswith(".wvd"):
        raise AmazonMusicConfigError(
            f"Amazon Music: wvd_path must point to a .wvd file, got: {path}"
        )
    return resolved


@dataclasses.dataclass(slots=True)
class PSSHEntitlements:
    katana: typing.Optional[PSSH] = None
    """ Unlimited subscription tier (UHD, HD, SD, LD) """
    robin: typing.Optional[PSSH] = None
    """ Amazon Prime subscription tier """
    hawkfire: typing.Optional[PSSH] = None
    """ Free tier (SD) """
    nightwing: typing.Optional[PSSH] = None
    """ For AAC acquisition (all tiers have this) """
    sonic: typing.Optional[PSSH] = None
    """ Apart of Prime Music (SD)"""

    def to_dict(self):
        return dataclasses.asdict(self)
    
    def iterate(self):
        for name, item in (
            (field.name, getattr(self, field.name))
            for field in dataclasses.fields(self)
        ):
            if not isinstance(item, PSSH):
                continue
            yield (name, item)
    
    def _get_entitlements_group_id(self):
        # me when the method i want to use is literally a pain to implement:
        for n, entitlement in self.iterate():
            if not isinstance(entitlement, PSSH):
                continue
            pssh = WidevinePsshData()
            pssh.ParseFromString(entitlement.init_data)
            yield pssh.group_ids[0].decode()
    
    @property
    def music_territory(self):
        group_id = next(self._get_entitlements_group_id(), None)
        if not group_id:
            return
        
        return group_id[-2:]

    @property
    def entitlement_names(self):
        for group_id in self._get_entitlements_group_id():
            if not group_id:
                continue
            
            yield group_id.split(":", maxsplit=1)[0]

@dataclasses.dataclass(slots=True)
class AudioTrack:
    asin: str
    codec: CodecEnum
    bitrate: int
    """ As kilobits per second (kbps) """
    sample_rate: float
    url: str
    official_quality_name: str
    quality: str
    quality_ranking: int
    entitlements: PSSHEntitlements
    web_pssh: PSSH
    # pssh: typing.Any
    reference_loudness: Optional[str] = None
    """ e.g 7.2 LUFS, some manifests don't include it """
    bit_depth: Optional[int] = None

    def to_dict(self):
        return dataclasses.asdict(self)


# This is a Amazon Music module for OrpheusDL, requires an active account (optionally requires a active paid subscription)

module_information = ModuleInformation(  # Only service_name and module_supported_modes are mandatory
    service_name="Amazon Music",
    module_supported_modes=ModuleModes.download
    | ModuleModes.lyrics
    | ModuleModes.covers
    | ModuleModes.credits,
    # flags = ModuleFlags.hidden,
    # Flags:
    # startup_load: load module on startup
    # hidden: hides module from CLI help options
    # jwt_system_enable: handles bearer and refresh tokens automatically, though currently untested
    # private: override any public modules, only enabled with the -p/--private argument, currently broken
    global_settings={
        "wvd_path": "",
        "force_non_spatial": False,
        "prefer_spatial_mha1": False,
        "prefer_spatial_ac4": False,
        "prefer_aria2c": False,
        "prefer_removal_of_device_when_revoked": True,
        "prefer_account_continent": "NA", # or FE (Asia) or EU (Europe)
        "trim_track_by_sample_rate": True,
        "max_track_quality_to_use": "",
        "quality_format": "{sample_rate}kHz/{bit_depth}bit",
        "use_own_master_keys": False,
        "master_keys": {
            "TIER (KATANA/ROBIN):ISO 3166-1 Alpha-2 Country Code": "64 character key string in hex"
        },
        "force_login": False,
    },
    global_storage_variables=[],
    session_settings={
        "email": "",
        "password": "",
        "country": "",
    },
    session_storage_variables=["credentials"],
    netlocation_constant="amazon",
    test_url="https://music.amazon.com/albums/B08TZPYLJN",
    login_behaviour=ManualEnum.manual,  # setting to ManualEnum.manual disables Orpheus automatically calling login() when needed
    url_decoding=ManualEnum.manual,
)


def validate_amazonmusic_setup(settings: dict, session_storage_path: str, *, gui_mode: bool = False) -> None:
    """Raise AmazonMusicConfigError when required setup is missing (before module init)."""
    if not isinstance(settings, dict):
        settings = {}

    missing: list[str] = []
    wvd_path = _normalize_wvd_path(settings.get("wvd_path"))
    if not wvd_path:
        missing.append("wvd_path — Widevine device file (.wvd)")
    else:
        _validate_wvd_path(wvd_path)

    logged_in = ModuleInterface.has_cached_credentials(session_storage_path)
    country = str(settings.get("country") or "").strip()
    if not logged_in and not country:
        missing.append("country — two-letter Amazon store country (e.g. US, DE, NL)")

    if missing:
        where = "Settings > Amazon Music" if gui_mode else "settings.json (modules.amazonmusic)"
        lines = "\n  • ".join(missing)
        extra = ""
        if not logged_in:
            extra = (
                "\n  • Sign in — complete the browser login in the Amazon Music settings tab"
                if gui_mode
                else "\n  • Sign in — complete browser login in the GUI or CLI"
            )
        raise AmazonMusicConfigError(
            "Amazon Music is not set up yet. Before searching or downloading, configure:\n"
            f"  • {lines}{extra}\n\n"
            f"Configure these in {where}."
        )


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        self.settings = module_controller.module_settings
        self.options = module_controller.orpheus_options
        self.module_controller = module_controller
        self.print = module_controller.printer_controller.oprint
        self.tsc = module_controller.temporary_settings_controller

        # Items highest are iterated first
        self.quality_parse = {
            QualityEnum.MINIMUM: [
                "LD",
            ],
            QualityEnum.LOW: [
                "SD_LOW",
            ],
            QualityEnum.MEDIUM: [
                "SD_MEDIUM",
            ],
            QualityEnum.HIGH: [
                "SD_HIGH",
            ],
            QualityEnum.LOSSLESS: [
                "HD",
            ],
            QualityEnum.HIFI: ["UHD"],
        }
        if not self.settings.get("force_non_spatial", False):
            self.quality_parse[QualityEnum.HIFI] = [
                "UHD",
                "SPATIAL_RA360",
                "SPATIAL_ATMOS",
            ]
        
        self.tier_parse = {
            AmazonMusicTier.FREE: QualityEnum.MINIMUM,
            AmazonMusicTier.PRIME: QualityEnum.HIGH,
            AmazonMusicTier.UNLIMITED: QualityEnum.HIFI,
        }

        # if not module_controller.orpheus_options.disable_subscription_check and (
        #     self.quality_parse[module_controller.orpheus_options.quality_tier]
        #     > self.mobile_session.get_user_tier()
        # ):
        #     print(
        #         "Example: quality set in the settings is not accessible by the current subscription"
        #     )

        wvd_path = _validate_wvd_path(self.settings.get("wvd_path"))
        self.settings["wvd_path"] = wvd_path
        self.cdm = Cdm.from_device(Device.load(wvd_path))

        if self.settings.get("force_login", False) or not self.load_cached_mobile_session(check_only=True):
            mobile_session = self.login_onto_mobile(
                self.settings.get("email", ""),
                self.settings.get("password", ""),
            )

    def login_onto_mobile(
        self, email: str, password: str
    ):  # Called automatically by Orpheus when standard_login is flagged, otherwise optional
        country = str(self.settings.get("country") or "").strip()
        if not country:
            raise AmazonMusicConfigError(
                "Amazon Music: Country is required before login.\n"
                "Set a two-letter ISO country code (e.g. US, BE, NL) in settings.json or the GUI Amazon Music tab."
            )
        self.print(
            f"\n{module_information.service_name}: Logging in using "
            f"{AmazonRegion.get_region_by_country(self.settings['country']).pretty_name} as the region.\n"
        )
        oauth_flow = self.module_controller.get_gui_handler("amazonmusic_oauth_flow")
        mobile_session = AmazonMusicMobileAPI.login_via_mobile(
            email,
            password,
            self.settings["country"],
            oauth_flow_callback=oauth_flow,
        )
        if not isinstance(mobile_session, AmazonMusicMobileAPI):
            # die
            raise TypeError("Login failed")

        LOGGER.debug(mobile_session.retrieve_capability())
        self.update_cached_credentials(mobile_session.credentials)

        return mobile_session
    
    def update_cached_credentials(self, credentials: AmazonMusicMobileAPICredentials, remove_arg_credentials: typing.Optional[bool] = False):
        cached_creds = self.tsc.read("credentials") or {}
        # Add support for previous versions of this module
        if isinstance(cached_creds, dict) and AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds):
            cached_creds = {
                cached_creds["web_client_config"]["music_territory"]: cached_creds
            }
        if remove_arg_credentials:
            if list(cached_creds.keys()).count(credentials.account_region.country) > 1:
                self.print(
                    f"{module_information.service_name}: "
                    f"There are multiple cached account credentials for {credentials.account_region.pretty_name}, "
                    "deleting the most recent one."
                )
            cached_creds.pop(credentials.account_region.country, None)
            final_creds = cached_creds
        else:
            final_creds = cached_creds | {
                credentials.account_region.country: 
                credentials.to_dict()
            }
        return self._save_credentials_to_tsc(final_creds)
    
    def _save_credentials_to_tsc(self, credentials: dict):
        # Save credentials to loginstorage.bin
        try: 
            self.tsc.set(
                "credentials",
                credentials
            )
        except Exception:
            return False
        return True

    @staticmethod
    def has_cached_credentials(session_storage_path: str) -> bool:
        """True when loginstorage.bin has a parseable Amazon Music session (logged in)."""
        cached_creds = (
            read_temporary_setting(
                session_storage_path, "amazonmusic", "custom_data", "credentials"
            )
            or {}
        )
        if not isinstance(cached_creds, dict) or not cached_creds:
            return False
        try:
            if AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds):
                creds_dict = cached_creds
            else:
                country_keys = [
                    k
                    for k, v in cached_creds.items()
                    if isinstance(v, dict)
                    and AmazonMusicMobileAPICredentials.is_dict_of_instance(v)
                ]
                if not country_keys:
                    return False
                creds_dict = cached_creds[country_keys[0]]
            if not str(creds_dict.get("refresh_token") or "").strip():
                return False
            AmazonMusicMobileAPICredentials.from_dict(creds_dict)
            return True
        except Exception:
            return False

    def load_cached_mobile_session(
        self,
        selected_region: typing.Optional[AmazonRegion] = None,
        use_exact_region: typing.Optional[bool] = False,
        check_only: typing.Optional[bool] = False
    ):
        cached_creds = self.tsc.read("credentials") or {}
        if not cached_creds:
            return
        # for debug
        # pprint.pprint(cached_creds)

        if selected_region and selected_region.country not in cached_creds and use_exact_region:
            return
        if not selected_region or (
            selected_region.country not in cached_creds
            and not AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds)
        ):
            selected_region = AmazonRegion.get_region_by_country(list(cached_creds.keys())[0])
        if not selected_region:
            return

        credentials = (
            AmazonMusicMobileAPICredentials.from_dict(
                cached_creds[selected_region.country]
                if not AmazonMusicMobileAPICredentials.is_dict_of_instance(cached_creds)
                and selected_region.country in cached_creds
                else cached_creds
            ) if cached_creds else None
        )
        if not credentials:
            return
        if check_only is True:
            return True

        mobile_session = AmazonMusicMobileAPI(credentials=credentials)
        
        if mobile_session.credentials.access_token_expired:
            try:
                mobile_session.refresh_access_token()
            except httpx.HTTPError as he:
                LOGGER.error(he)
                self.print(
                    f"{module_information.service_name}: "
                    f"This account (name: {mobile_session.credentials.customer_info.get('name', 'Unknown user')}, "
                    f"country: {mobile_session.credentials.account_region.pretty_name}) "
                    f"device session/account may be deleted.")
                
                if self.settings["prefer_removal_of_device_when_revoked"] is True:
                    self.update_cached_credentials(credentials, remove_arg_credentials=True)
                    self.print(f"{module_information.service_name}: Deleted from cache as per your settings.")
                    return
                while True:
                    user_input = input(
                        f"{module_information.service_name}: "
                        "Would you like to remove the current device for the account region "
                        f"{mobile_session.credentials.account_region.pretty_name!r}? (Y/N): "
                    )
                    if "Y" in user_input.upper():
                        self.print(f"{module_information.service_name}: Deleting from cache..")
                        self.update_cached_credentials(credentials, remove_arg_credentials=True)
                        return
                    elif "N" in user_input.upper():
                        self.print(f"{module_information.service_name}: Skipped.")
                        return
                    else:
                        self.print(f"{module_information.service_name}: Invalid input, please try again.")
                        continue

            else:
                self.update_cached_credentials(mobile_session.credentials)

        LOGGER.debug(mobile_session.credentials)
        LOGGER.debug(mobile_session.get_account_subscription_tier())
        # print(pprint.pformat(mobile_session.retrieve_customer_home()))
        # print(pprint.pformat(mobile_session.get_account_status()))

        return mobile_session
    
    def select_usable_region_from_regions(self, regions: typing.Iterable[AmazonRegion]):
        if not regions:
            return
        cached_creds = self.tsc.read("credentials") or {}
        if not cached_creds:
            return
        for region in regions:
            if region.country not in cached_creds:
                continue
            return region
        return
    
    @functools.lru_cache
    def select_session(self, selected_region: AmazonRegion):
        # select by country
        session = self.load_cached_mobile_session(selected_region, use_exact_region=True)
        
        if not session:
            # select by continent (NA, EU, FE)
            continents = [selected_region.region] + [item for item in AmazonContinent if selected_region.region != item]
            for continent in continents:
                new_region = self.select_usable_region_from_regions(AmazonRegion.get_available_regions_by_continent(continent.name))
                if not new_region:
                    continue
                session = self.load_cached_mobile_session(
                    new_region,
                    use_exact_region=True
                )
                if not session:
                    continue
                self.print(
                    f"\n{module_information.service_name}: Using a {new_region.pretty_name!r} account "
                    f"for the region {selected_region.pretty_name!r} "
                    f"(you are not logged into that region directly).\n"
                )
                
                selected_region = new_region
                break
        if not isinstance(session, AmazonMusicMobileAPI):
            raise ValueError("No accounts logged in!")
        
        entitlement_usage = (
            self.settings['use_own_master_keys']
            and any(
                str(item).endswith(selected_region.country)
                for item in self.settings['master_keys']
            )
        )
        self.print(
            f"{module_information.service_name}: Selected account region: {selected_region.pretty_name!r}\n"
            f"  Subscription: {session.credentials.tier.name.title()!r}\n"
            f"  Entitlement master keys: {'enabled' if entitlement_usage else 'disabled'}\n"
        )

        return session, selected_region

    def custom_url_parse(self, link: str) -> MediaIdentification:
        url = urlparse(link)
        queries = parse_qs(url.query)
        
        music_territory_name = next(iter(queries.get("musicTerritory", [])), None)
        music_territory = None
        if music_territory_name:
            music_territory = AmazonRegion.get_region_by_country(music_territory_name)
        if not music_territory:
            music_territory = AmazonRegion.get_available_regions_by_continent(self.settings["prefer_account_continent"])[0]
        mobile_session, _ = self.select_session(music_territory)

        # if not (url.netloc.endswith(mobile_session.credentials.tld)) and not self.settings["use_own_master_keys"]:
        #     raise ValueError(
        #         f"You must provide a URL that is within the same region as the account!"
        #     )
        
        if track_item := queries.get("trackAsin"):
            return MediaIdentification(
                DownloadTypeEnum.track,
                media_id=track_item[0],
                extra_kwargs={
                    "media_region": music_territory
                }
            )

        components = [item.strip("\\") for item in url.path.split("/") if item]

        # use the same logic as orpheusdl cuz lazy
        url_constants = {
            "tracks": DownloadTypeEnum.track,
            "albums": DownloadTypeEnum.album,
            "playlists": DownloadTypeEnum.playlist,
            "user-playlists": DownloadTypeEnum.playlist,
            "artists": DownloadTypeEnum.artist,
        }

        type_matches = [
            media_type
            for url_check, media_type in url_constants.items()
            if url_check in components
        ]

        if len(components) > 2:
            return MediaIdentification(
                media_type=type_matches[-1],
                media_id=components[1]
                if type_matches[-1] is DownloadTypeEnum.artist
                else components[2],  # my/playlists/uuid
                extra_kwargs={
                    "media_region": music_territory
                }
            )
        else:
            return MediaIdentification(
                media_type=type_matches[-1],
                media_id=components[-1],
                extra_kwargs={
                    "media_region": music_territory
                }
            )

    def get_track_info(
        self,
        track_id: str,
        quality_tier: QualityEnum,
        codec_options: CodecOptions,
        media_region: AmazonRegion,
        data: dict = {},
    ) -> TrackInfo:  # Mandatory
        mobile_session, _ = self.select_session(media_region)

        if (
            codec_options.spatial_codecs is False
            and not self.settings["force_non_spatial"]
        ):
            self.print(
                f"{module_information.service_name}: Warning: force_non_spatial is not set to True in settings.json"
            )
            self.print(f"Spatial codecs will be downloaded unless this is changed!")
        try:
            mapped_audio_tracks = (
                data[f"{track_id}_quality_mapping"]
                if data and f"{track_id}_quality_mapping" in data
                else None
            )

            if not mapped_audio_tracks:
                asin, mpd = mobile_session.get_track_manifest(
                    track_asin=track_id,
                    force_3d=any(
                        not self.settings["force_non_spatial"] or self.settings[key]
                        for key in ("prefer_spatial_mha1", "prefer_spatial_ac4")
                    ),
                    region_to_use=media_region
                )
                if not (asin and mpd):
                    raise RuntimeError(
                        f"Failed to obtain track manifest for {track_id}"
                    )
                mapped_audio_tracks = self.mpd_to_quality_map(
                    mpd,
                    asin,
                    mobile_session.credentials.tier,
                    media_region,
                    mobile_session.credentials.account_region
                )

            track_to_use = self._get_usable_audio_track_of_mapped_quailty(
                mapped_audio_tracks=mapped_audio_tracks,
                quality_tier=QualityEnum(min(quality_tier.value, self.tier_parse[mobile_session.credentials.tier].value)) if not self.options.disable_subscription_check and not self.settings["use_own_master_keys"] else quality_tier,
                to_print=True,
            )

            track_data = (
                data[track_id]
                if data and track_id in data
                else mobile_session.get_track_info(track_id, region_to_use=media_region) # music_territory="CA"
            )
            album_id = str(track_data["album"]["asin"])
            album_data = (
                data[album_id]
                if data and album_id in data
                else mobile_session.get_album_info(album_id, region_to_use=media_region) # music_territory="NZ"
            )
            album_id = str(album_data["asin"])
            
            # if playlist_track_metadata := data.get(f"{track_id}_playlist"):
            #     import pprint
            #     pprint.pprint(playlist_track_metadata)

            # NOTE: I *could* use catalog_track AND catalog album
            # but I prefer to limit the amount of API Requests per track
            search_data = (
                data[f"{album_id}_search"]
                if data and f"{album_id}_search" in data
                else mobile_session.search(
                    query='"{}", "{}"'.format(
                        album_data["artist"]["name"],
                        album_data["title"],
                        # track_data["title"],
                    ),
                    asins=(album_id, track_id, str(track_data["globalAsin"])),
                    search_types=("catalog_album",),
                    region_to_use=media_region
                )
                or {}
            )
            # page_entity_data = (
            #     dict(data[f"{album_id}_page_entity_data"])
            #     if data and f"{album_id}_page_entity_data" in data
            #     else mobile_session.get_page(f"album/{album_id}", count=0, locale="en_US").get("entity", {})
            # )

            release_datetime = self._get_date_from_metadata(album_data)

            artists: list[str] = []
            contributor_asins = tuple(track_data["artist"]["contributorAsins"])
            # Most of the time there are more than one version of the main artist
            # e.g the main artist with all the albums is track_data["artist"]["asin"]
            # meanwhile there exists a alternative version of the same artist inside (no albums)
            # track_data["artist"]["contributorAsins"] (usually only one item)
            # e.g https://music.amazon.co.jp/albums/B08P688B62
            if len(contributor_asins) > 1:
                artists.extend(
                    str(item.get("name"))
                    for item in mobile_session.get_metadata(contributor_asins, region_to_use=media_region)[
                        "artistList"
                    ]
                    if item.get("name")
                )
            else:
                # Fallback, include the formatted artist name (might include contributors)
                artists.append(track_data["artist"]["name"])

            # Assumes the primaryArtistName is one single individual name, not concanated
            primary_artist_name = album_data.get("primaryArtistName", "")
            # Attempt to seperate each contributor if they're concatenated
            for artist in artists.copy():
                if sep_artists := self.parse_credit_names_from_name(artist):
                    if artist in artists:
                        artists.remove(artist)
                    for new_art in sep_artists:
                        if not primary_artist_name:
                            continue
                        if (
                            (
                                new_art in primary_artist_name
                                or
                                primary_artist_name in new_art
                            ) and
                            new_art != primary_artist_name
                        ):
                            # Fails here when an artist thats split
                            # isn't supposed to be split
                            continue
                        artists.append(new_art)

            if primary_artist_name in artists:
                artists.remove(primary_artist_name)

            # Remove duplicates
            artists = natsort.natsorted(set(artists))

            # Prefer to have the primary artist name at the start
            artists.insert(0, primary_artist_name)

            # Calculate the total disc avaliable by iterating each track and using the highest value
            disc_total = max(int(t["discNum"]) for t in album_data.get("tracks", [{}]))
            # Sanitize writers
            writers = [str(item).strip() for item in track_data.get("songWriters", []) if item]

            for name in writers.copy():
                if names := self.parse_credit_names_from_name(name):
                    if name in writers:
                        writers.remove(name)
                    writers.extend(names)

            composers = natsort.natsorted(set(writers))

            composers = "; ".join(composers)

            url = f"https://music.amazon.{media_region.domain_tld}/albums/{album_id}?trackAsin={track_id}&musicTerritory={media_region.country}"
            extra_tags = {
                "Composer": composers,  # force set the composer tag, because orpheus doesn't handle it
                "WWW": url,
            }

            if merchant_name := album_data.get("productDetails", {}).get(
                "merchantName"
            ):
                extra_tags.update(
                    {
                        "Merchant": " ".join(
                            str(
                                merchant_name
                            ).split()  # Remove whitespaces, if any (e.g Amazon Music USA)
                        )
                    }
                )
            if track_to_use.reference_loudness:
                extra_tags.update(
                    {
                        "REPLAYGAIN_REFERENCE_LOUDNESS": track_to_use.reference_loudness  # ref. https://wiki.hydrogenaud.io/index.php?title=ReplayGain_2.0_specification
                    }
                )

            product_details = album_data.get("productDetails") or {}

            # sanitize and format the genre name from search data
            # this genre tends to be more specific however,
            # it may not be always avaliable
            # e.g https://music.amazon.co.jp/albums/B09JNRPVHN?trackAsin=B09JNRQQ3P
            genres = sorted(
                list(
                    item
                    for item in set(
                        self.parse_genres_from_genre(search_data.get("primaryGenre", ""))
                        | self.parse_genres_from_genre(
                            product_details.get("primaryGenreName", "")
                        )
                    )
                    if item
                ),
                key=len
            )
            tags = Tags(
                album_artist=primary_artist_name,
                composer=composers,
                copyright=product_details.get("copyright"),
                isrc=track_data.get("isrc"),
                # upc="",
                disc_number=int(track_data["discNum"] or 1),
                total_discs=disc_total,
                track_number=int(track_data["trackNum"] or 1),  # None/0/1 if no discs
                total_tracks=int(album_data["trackCount"] or 1),  # None/0/1 if no discs
                # replay_gain=0.0,
                # replay_peak=0.0,
                genres=genres,
                label=product_details.get("label"),
                release_date=release_datetime.strftime(
                    "%Y-%m-%d"
                ),  # Format: YYYY-MM-DD
                # comment=comment,
                extra_tags=extra_tags,
            )

            valid_album_asins = [album_id]
            
            if asin := album_data.get("requestedAsin"):
                valid_album_asins.append(asin)
            if asin := album_data.get("globalAsin"):
                valid_album_asins.append(asin)

            cover_url, search_data = self.get_hi_res_cover(
                asins=valid_album_asins,
                query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
                search_data=search_data,
                mobile_session=mobile_session,
                media_region=media_region
            )
            if not cover_url:
                # Default 600 x 600 cover art
                cover_url = str(album_data.get("image", ""))
            else:
                cover_url = self.format_cover_url(
                    cover_url, self.options.default_cover_options
                )
            
            return TrackInfo(
                name=self.sanitize_parental_status_name(track_data["title"]),
                album_id=f"{album_id}_{media_region.country}",
                album=self.sanitize_parental_status_name(track_data["album"]["title"]),
                artists=artists,
                tags=tags,
                codec=track_to_use.codec,
                cover_url=cover_url,  # make sure to check module_controller.orpheus_options.default_cover_options
                release_year=int(release_datetime.strftime("%Y")),
                explicit=track_data["parentalControls"]["hasExplicitLanguage"],
                artist_id=track_data.get("artist", {}).get("asin", ""),  # optional
                duration=track_data["duration"],
                # animated_cover_url="",  # optional
                # description="",  # optional
                bit_depth=track_to_use.bit_depth,  # optional
                sample_rate=track_to_use.sample_rate,  # optional
                bitrate=track_to_use.bitrate,  # optional
                download_extra_kwargs={
                    "audio_track": track_to_use,
                    "media_region": media_region,
                },  # optional only if download_type isn't DownloadEnum.TEMP_FILE_PATH, whatever you want
                cover_extra_kwargs={
                    "media_region": media_region
                    # "data": {track_id: ""}
                },  # optional, whatever you want, but be very careful
                credits_extra_kwargs={
                    "media_region": media_region
                    # "data": {track_id: ""}
                },  # optional, whatever you want, but be very careful
                lyrics_extra_kwargs={
                    "media_region": media_region
                    # "data": {track_id: ""}
                },  # optional, whatever you want, but be very careful
                error="",  # only use if there is an error
            )
        except Exception as e:
            LOGGER.error(e, exc_info=True)
            raise e

    def get_track_download(self, audio_track: AudioTrack, media_region: AmazonRegion, **kwargs):
        mobile_session, _ = self.select_session(media_region)
        # print(audio_track)
        session_id = None
        try:
            os.makedirs("temp/", exist_ok=True)
            encrypted_track_location = f"{create_temp_filename()}.mp4"

            self.download(
                audio_track.url,
                encrypted_track_location,
                use_aria2c=self.settings["prefer_aria2c"],
            )

            decrypted_track_location = f"{create_temp_filename()}.mp4"  # {codec_data[audio_track.codec].container.name}
            if audio_track.entitlements.music_territory and audio_track.entitlements.music_territory != media_region.country:
                self.print(
                    f'{module_information.service_name}: Entitlements are '
                    f'for {audio_track.entitlements.music_territory}, '
                    f'instead of {media_region.country}'
            )
            if (
                self.settings["use_own_master_keys"]
                and any(str(item).endswith(media_region.country) for item in self.settings["master_keys"])
            ):
                while True:
                    selected_pssh = None
                    main_key: bytes = b""
                    for name, entitle_pssh in audio_track.entitlements.to_dict().items():
                        if not isinstance(entitle_pssh, PSSH):
                            continue
                        key_name = f"{name.upper()}:{audio_track.entitlements.music_territory}"
                        if key_name not in self.settings["master_keys"]:
                            continue

                        main_key = bytes.fromhex(self.settings["master_keys"][key_name])
                        if main_key:
                            selected_pssh = entitle_pssh
                        break
                    
                    if not (main_key and selected_pssh):
                        break
                    
                    key_id, key = self.get_decrypted_key(
                        enc_key=main_key,
                        pssh=selected_pssh,
                        enc_key_type="CONTENT",
                    )
                    # print(f"using own entitlements {key_id=} {key=}")
                    self.call_shaka_packager(
                        encrypted_file=encrypted_track_location,
                        destination_file=decrypted_track_location,
                        key_id=key_id,
                        key=key,
                        label=audio_track.official_quality_name
                    )
                    break
                    
            # Interact with the license server
            if not os.path.exists(decrypted_track_location) and mobile_session.credentials.tier is not AmazonMusicTier.FREE:
                session_id = self.cdm.open()
                # print(f"{audio_track.entitlements=} {audio_track.web_pssh=}")
                
                used_entitlement: dict[str, PSSH] | None = {}
                for name, pssh_to_test in audio_track.entitlements.to_dict().items():
                    if not self.cdm.system_id == 9780:
                        continue
                    if not pssh_to_test:
                        continue
                    if f"{name.upper()}_CONTENT" not in mobile_session.credentials.tier.internal_content_tiers and not self.options.disable_subscription_check:
                        continue
                    
                    # maybe?
                    # if audio_track.entitlements.music_territory != mobile_session.credentials.account_region.country:
                    #     continue

                    license_challenge = base64.b64encode(
                        self.cdm.get_license_challenge(
                            session_id, pssh_to_test, privacy_mode=False
                        )
                    ).decode("utf-8")

                    license_response = None
                    try:
                        license_response = mobile_session.get_license_response(
                            asin=audio_track.asin, challenge=license_challenge, drm_type="WIDEVINE_ENTITLEMENT"
                        )
                    except (ValueError, httpx.HTTPStatusError) as e:
                        self.print(f"{module_information.service_name}: Failed entitlement master key acquisition.")
                        continue
                    else:
                        if not license_response:
                            # license_response = input("License retrieval failed, enter response here (for testing): ")
                            continue
                        # print(f"used entitle acq: {name}")
                        used_entitlement.update({
                            name: pssh_to_test
                        })
                        self.cdm.parse_license(session_id, license_response)
                        break
                else:
                    # print("reg")
                    license_challenge = base64.b64encode(
                        self.cdm.get_license_challenge(
                            session_id, audio_track.web_pssh, privacy_mode=False
                        )
                    ).decode("utf-8")

                    license_response = mobile_session.get_license_response(
                        asin=audio_track.asin, challenge=license_challenge, drm_type="WIDEVINE"
                    )
                    if not license_response:
                        raise ValueError("Failed to communicate with the license server")

                    self.cdm.parse_license(session_id, license_response)

                # print(used_entitlement)
                for key in self.cdm.get_keys(session_id):
                    if key.type == "ENTITLEMENT":
                        if not used_entitlement:
                            continue

                        name, used_pssh = used_entitlement.popitem()
                        # name , used_pssh = used_entitlement[next(iter(used_entitlement))]
                        key_id, dec_key = self.get_decrypted_key(
                            key.key,
                            used_pssh,
                            "CONTENT"
                        )
                        key_name = f"{name.upper()}:{audio_track.entitlements.music_territory}"
                        # if not self.settings["master_keys"].get("key_name"):
                        self.print(
                            f'{module_information.service_name}: New entitlement key '
                            f'found for {audio_track.entitlements.music_territory}! '
                            f'"{key_name}": "{key.key.hex()}"'
                        )

                    elif key.type == "CONTENT":
                        key_id = key.kid.hex
                        dec_key = key.key.hex()
                    else:
                        continue
                    # print(f"{key_id=} {dec_key=}")
                    # continue
                        
                    self.call_shaka_packager(
                        encrypted_file=encrypted_track_location,
                        destination_file=decrypted_track_location,
                        key_id=key_id,
                        key=dec_key,
                        label=audio_track.quality.upper()
                    )

            if not os.path.exists(decrypted_track_location):
                raise FileNotFoundError("Unable to decrypt the downloaded media file")

            LOGGER.debug("Ok with decryption")

            silentremove(encrypted_track_location)

            selected_codec_data = codec_data[audio_track.codec]

            if selected_codec_data.spatial:
                return TrackDownloadInfo(
                    download_type=DownloadEnum.TEMP_FILE_PATH,
                    temp_file_path=decrypted_track_location,
                    different_codec=audio_track.codec,
                )

            LOGGER.debug(
                f"Using {selected_codec_data.container.name} as the container for {audio_track.codec.name}"
            )
            final_decrypted_track_location = (
                f"{create_temp_filename()}.{selected_codec_data.container.name}"
            )
            # return
            ffcmd: ffmpeg = ffmpeg.input(decrypted_track_location)  # type: ignore
            ffcmd_out_kwargs = {
                "filename": final_decrypted_track_location,
                "loglevel": "warning",
                "audio_bitrate": f"{audio_track.bitrate}k",
            }
            if self.settings["trim_track_by_sample_rate"]:
                # self.print(f"Trimming out the first {int(int(audio_track.sample_rate) * 6.5)} samples")
                ffcmd_out_kwargs.update(
                    {"af": f"atrim=start_sample={int(int(audio_track.sample_rate) * 6.5)}"}
                )

            ffcmd = ffcmd.output(**ffcmd_out_kwargs)
            stdout, stderr = ffcmd.run()
            if stderr:
                raise RuntimeError(f"ffmpeg: {stderr}")
            silentremove(decrypted_track_location)


        finally:
            if session_id:
                self.cdm.close(session_id)

        return TrackDownloadInfo(
            download_type=DownloadEnum.TEMP_FILE_PATH,
            temp_file_path=final_decrypted_track_location,
        )

    def get_preview_audio_path(
        self,
        track_id: str,
        media_region: typing.Optional[AmazonRegion] = None,
        media_region_country: typing.Optional[str] = None,
    ) -> typing.Optional[str]:
        """
        Decrypt the full track at low quality for in-app search preview playback.
        Returns a local temp file path (not an HTTP URL). Results are cached per session.
        """
        if not track_id or not str(track_id).strip():
            return None

        if media_region is None and media_region_country:
            try:
                media_region = AmazonRegion.get_region_by_country(str(media_region_country).strip().upper())
            except Exception:
                media_region = None

        if media_region is None:
            continents = AmazonRegion.get_available_regions_by_continent(
                self.settings["prefer_account_continent"]
            )
            media_region = (
                self.select_usable_region_from_regions(continents)
                or (continents[0] if continents else None)
            )
        if media_region is None:
            return None

        cache_key = f"{track_id}:{media_region.country}"
        cache = getattr(self, "_preview_audio_cache", None)
        if cache is None:
            cache = {}
            self._preview_audio_cache = cache
        cached = cache.get(cache_key)
        if cached and os.path.isfile(cached):
            return cached

        mobile_session, media_region = self.select_session(media_region)
        asin, mpd = mobile_session.get_track_manifest(
            track_asin=track_id,
            force_3d=False,
            region_to_use=media_region,
        )
        if not (asin and mpd):
            return None

        mapped_audio_tracks = self.mpd_to_quality_map(
            mpd,
            asin,
            mobile_session.credentials.tier,
            media_region,
            mobile_session.credentials.account_region,
        )
        preview_tier = QualityEnum.MINIMUM
        audio_track = self._get_usable_audio_track_of_mapped_quailty(
            mapped_audio_tracks=mapped_audio_tracks,
            quality_tier=QualityEnum(
                min(
                    preview_tier.value,
                    self.tier_parse[mobile_session.credentials.tier].value,
                )
            )
            if not self.options.disable_subscription_check
            else preview_tier,
            to_print=False,
        )
        if not isinstance(audio_track, AudioTrack):
            return None

        download_info = self.get_track_download(audio_track, media_region)
        if (
            download_info
            and download_info.download_type is DownloadEnum.TEMP_FILE_PATH
            and download_info.temp_file_path
            and os.path.isfile(download_info.temp_file_path)
        ):
            cache[cache_key] = download_info.temp_file_path
            return download_info.temp_file_path
        return None

    @staticmethod
    def _metadata_artist_name(entity: dict) -> str:
        """Artist display name from album/track metadata or text-search hits."""
        if not entity:
            return ""
        artist = entity.get("artist")
        if isinstance(artist, dict) and artist.get("name"):
            return str(artist["name"])
        for key in ("primaryArtistName", "artistName", "albumArtistName"):
            if entity.get(key):
                return str(entity[key])
        return ""

    @staticmethod
    def _format_sample_rate_khz(sample_rate: typing.Any) -> str:
        try:
            sr = float(sample_rate)
        except (TypeError, ValueError):
            return ""
        if sr <= 0:
            return ""
        if sr == int(sr):
            return str(int(sr))
        return f"{sr:g}"

    @staticmethod
    def _is_spatial_quality_display(codec_name: str, official_name: str) -> bool:
        codec_lower = (codec_name or "").lower()
        official_upper = (official_name or "").strip().upper()
        if official_upper in ("3D", "ATMOS"):
            return True
        return any(
            hint in codec_lower
            for hint in (
                "e-ac-3",
                "ac-4",
                "mpeg-h 3d",
                "mha1",
                "mhm1",
                "dolby atmos",
                "360",
            )
        )

    @staticmethod
    def _spatial_quality_label(quality_tags: dict) -> str:
        official = str(quality_tags.get("official_quality_name") or "").strip()
        codec = str(quality_tags.get("codec_pretty_name") or "").strip()
        parts: list[str] = []
        if official:
            parts.append(official)
        if codec and codec not in parts:
            parts.append(codec)
        return " ".join(parts)

    @staticmethod
    def _amazon_spatial_display_label(codec_name: str = "", official_name: str = "") -> str:
        """Canonical spatial labels for search UI (small-caps via gui._to_small_caps)."""
        combined = f"{official_name} {codec_name}".lower()
        if re.search(r"mpeg-h|mha1|mhm1", combined):
            return "3D MPEG-H Audio"
        if re.search(r"e-ac-3|ac-4|\batmos\b|joc|dolby", combined):
            return "◗◖ ATMOS"
        if "360" in combined:
            return "360 Reality Audio"
        if (official_name or "").strip().upper() in ("3D", "ATMOS"):
            return "◗◖ ATMOS"
        raw = ModuleInterface._spatial_quality_label(
            {
                "official_quality_name": official_name,
                "codec_pretty_name": codec_name,
            }
        )
        return raw or "◗◖ ATMOS"

    def _quality_label_from_mapped(
        self,
        mapped_audio_tracks: dict,
        quality_tier: typing.Optional[QualityEnum] = None,
    ) -> str:
        tier = quality_tier or getattr(self.options, "quality_tier", None) or QualityEnum.HIGH
        best = self._get_usable_audio_track_of_mapped_quailty(mapped_audio_tracks, tier)
        return self._format_quality_display(
            {
                "official_quality_name": best.official_quality_name,
                "codec_pretty_name": codec_data[best.codec].pretty_name,
                "bit_depth": best.bit_depth,
                "sample_rate": best.sample_rate,
            },
            str(self.settings.get("quality_format", "")),
        )

    @staticmethod
    def _quality_display_sort_key(label: str) -> tuple[int, float, int]:
        """Sort key for picking the single best album-level quality label."""
        text = str(label or "").strip()
        low = text.lower()
        if re.search(r"opus\s+only", low):
            return (1, 0.0, 0)
        m = re.search(r"(\d+(?:\.\d+)?)\s*kHz\s*/\s*(\d+)\s*bit", text, re.I)
        if m:
            return (3, float(m.group(1)), int(m.group(2)))
        if ModuleInterface._is_spatial_quality_display(text, text):
            return (2, 48.0, 0)
        return (0, 0.0, 0)

    @staticmethod
    def _sample_track_asins_for_quality_probe(
        track_asins: list[str], max_probe: int = SEARCH_QUALITY_MAX_TRACK_PROBE
    ) -> tuple[str, ...]:
        """First / middle / last track — enough to detect the album's highest tier."""
        if not track_asins:
            return ()
        if len(track_asins) <= max_probe:
            return tuple(track_asins)
        indices = sorted({0, len(track_asins) // 2, len(track_asins) - 1})
        return tuple(track_asins[i] for i in indices[:max_probe])

    @staticmethod
    def _best_quality_display(displays: typing.Iterable[str]) -> str:
        """Highest available quality only (e.g. 44.1kHz/24bit, not 16bit + OPUS)."""
        labels = [str(d).strip() for d in displays if d and str(d).strip()]
        if not labels:
            return ""
        return max(labels, key=ModuleInterface._quality_display_sort_key)

    def _probe_quality_labels(
        self,
        mobile_session: AmazonMusicMobileAPI,
        track_asins: tuple[str, ...],
        media_region: AmazonRegion,
        force_3d: bool,
        quality_tier: QualityEnum,
    ) -> set[str]:
        labels: set[str] = set()
        if not track_asins:
            return labels
        for track_asin, manifest in mobile_session.get_tracks_manifest(
            track_asins,
            force_3d=force_3d,
            region_to_use=media_region,
        ):
            if manifest is None:
                continue
            mapped = self.mpd_to_quality_map(
                manifest,
                track_asin,
                mobile_session.credentials.tier,
                media_region,
                mobile_session.credentials.account_region,
            )
            label = self._quality_label_from_mapped(mapped, quality_tier)
            if label:
                labels.add(label)
        return labels

    @staticmethod
    def _pick_spatial_display_label(labels: typing.Iterable[str]) -> typing.Optional[str]:
        for label in labels:
            text = str(label)
            if ModuleInterface._is_spatial_quality_display(text, text):
                return text
        return None

    @staticmethod
    def _strip_legacy_bit_khz_suffix(text: str) -> str:
        """Remove template artifacts like 'bit-48kHz' left when bit_depth is empty."""
        return re.sub(
            r"\s*bit-?\s*\d+(?:\.\d+)?\s*khz\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    @staticmethod
    def _format_quality_display(quality_tags: dict, quality_format: str) -> str:
        """Human-readable quality (Qobuz-style kHz/bit); Opus SD has no bit depth."""
        codec_name = str(quality_tags.get("codec_pretty_name") or "").strip().lower()
        official_name = str(quality_tags.get("official_quality_name") or "").strip()
        bit_depth = quality_tags.get("bit_depth")
        sample_rate = quality_tags.get("sample_rate")
        if codec_name == "opus" and bit_depth is None:
            return "OPUS only"
        if ModuleInterface._is_spatial_quality_display(codec_name, official_name):
            return ModuleInterface._amazon_spatial_display_label(codec_name, official_name)
        if bit_depth is not None and sample_rate is not None:
            try:
                bd = int(bit_depth)
                sr_text = ModuleInterface._format_sample_rate_khz(sample_rate)
                if bd > 0 and sr_text:
                    return f"{sr_text}kHz/{bd}bit"
            except (TypeError, ValueError):
                pass
        fmt = {
            "official_quality_name": official_name,
            "codec_pretty_name": quality_tags.get("codec_pretty_name") or "",
            "bit_depth": bit_depth if bit_depth is not None else "",
            "sample_rate": ModuleInterface._format_sample_rate_khz(sample_rate),
        }
        try:
            out = str(quality_format or "").format(**fmt)
        except (KeyError, ValueError, TypeError):
            out = f"{official_name} {fmt['codec_pretty_name']}".strip()
        if re.search(r"\bopus\b", out, re.I) and re.search(r"none\s*bit|nonebit", out, re.I):
            return "OPUS only"
        return ModuleInterface._strip_legacy_bit_khz_suffix(out)

    @staticmethod
    def _quality_labels_from_entity(entity: dict) -> list[str]:
        """HD / UHD / Atmos from catalog contentEncoding (may not match actual MPD codecs)."""
        if not isinstance(entity, dict):
            return []
        encoding_labels = {
            "hdAvailable": "HD",
            "hdavailable": "HD",
            "HD": "HD",
            "hd": "HD",
            "uhdAvailable": "UHD",
            "uhdavailable": "UHD",
            "UHD": "UHD",
            "uhd": "UHD",
            "ULTRA_HD": "Ultra HD",
            "ULTRAHD": "Ultra HD",
            "ultraHdAvailable": "Ultra HD",
            "atmosAvailable": "Dolby Atmos",
            "atmosavailable": "Dolby Atmos",
            "ra360Available": "360 Reality Audio",
            "ra360available": "360 Reality Audio",
            "immersive": "Immersive Audio",
        }
        tags: list[str] = []

        def _add(label: str):
            if label and label not in tags:
                tags.append(label)

        def _label_for_key(key: str) -> typing.Optional[str]:
            if key in encoding_labels:
                return encoding_labels[key]
            kl = key.lower()
            if "uhd" in kl or "ultra" in kl:
                return "UHD"
            if kl == "hd" or kl.endswith("hd") or "highdefinition" in kl:
                return "HD"
            if "atmos" in kl:
                return "Dolby Atmos"
            if "360" in kl or "ra360" in kl:
                return "360 Reality Audio"
            if "immersive" in kl:
                return "Immersive Audio"
            return None

        enc = entity.get("contentEncoding")
        if isinstance(enc, dict):
            for key, val in enc.items():
                if val in (False, 0, None, "", "false", "False"):
                    continue
                _add(_label_for_key(str(key)) or str(key))
        elif isinstance(enc, list):
            for item in enc:
                if isinstance(item, dict):
                    key = str(item.get("type") or item.get("name") or item.get("encoding") or "")
                    if item.get("value") in (False, 0, None, "", "false", "False"):
                        continue
                    _add(_label_for_key(key) or str(key))
                elif isinstance(item, str):
                    try:
                        _add(encoding_labels[item])
                    except KeyError:
                        _add(_label_for_key(item) or item)

        for flag_key, label in (
            ("uhdAvailable", "UHD"),
            ("hdAvailable", "HD"),
            ("atmosAvailable", "Dolby Atmos"),
            ("ra360Available", "360 Reality Audio"),
        ):
            if entity.get(flag_key) in (True, "true", "True", 1):
                _add(label)

        audio_quality = entity.get("audioQuality") or entity.get("maximumAudioQuality")
        if isinstance(audio_quality, str):
            _add(_label_for_key(audio_quality) or audio_quality)

        return natsort.natsorted(tags, key=len)

    @staticmethod
    def _parse_khz_bit_display_label(label: str) -> typing.Optional[tuple[float, int]]:
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*kHz\s*/\s*(\d+)\s*bit",
            str(label or ""),
            re.IGNORECASE,
        )
        if not m:
            return None
        try:
            return float(m.group(1)), int(m.group(2))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_hi_res_lossless(sample_rate_khz: float, bit_depth: int) -> bool:
        """Same rule as Apple Music / Qobuz search: hi-res only above 48 kHz at 24-bit+."""
        return float(sample_rate_khz) > 48.0 and int(bit_depth) >= 24

    @staticmethod
    def _amazon_manifest_label_for_display(label: str) -> str:
        """MPD-derived label → ATMOS / kHz·bit / HI-RES / OPUS only (never catalog UHD flags)."""
        text = str(label or "").strip()
        if not text:
            return ""
        if ModuleInterface._is_spatial_quality_display(text, text):
            return ModuleInterface._amazon_spatial_display_label(text, "")
        if re.search(r"\bopus\s+only\b", text, re.IGNORECASE):
            return "OPUS only"
        parsed = ModuleInterface._parse_khz_bit_display_label(text)
        if parsed:
            sr_khz, bit_depth = parsed
            if ModuleInterface._is_hi_res_lossless(sr_khz, bit_depth):
                return "🅷 HI-RES"
            return text
        return text

    @staticmethod
    def _amazon_catalog_quality_display_labels(tags: typing.Iterable[str]) -> list[str]:
        """Fallback when manifest probe fails: never treat catalog UHD as HI-RES."""
        lower = [str(t).strip().lower() for t in tags if t]
        if not lower:
            return []
        if any("atmos" in tl or tl == "dolby atmos" for tl in lower):
            return ["◗◖ ATMOS"]
        if any("immersive" in tl for tl in lower):
            return ["IMMERSIVE AUDIO"]
        if any("360" in tl for tl in lower):
            return ["360 Reality Audio"]
        if any(tl == "hd" for tl in lower) or any(
            tl in ("uhd", "ultra hd", "ultra_hd", "ultrahd") or "ultra" in tl for tl in lower
        ):
            return ["FLAC"]
        return [str(t) for t in tags if t]

    @staticmethod
    def _quality_labels_include_atmos(labels: typing.Iterable[str]) -> bool:
        for label in labels or []:
            text = str(label)
            low = text.lower()
            if "atmos" in low or "◗" in text or "immersive" in low:
                return True
        return False

    @staticmethod
    def _catalog_atmos_display_label(entity: dict) -> typing.Optional[str]:
        """Atmos badge from catalog contentEncoding when manifest probe missed spatial tiers."""
        if not isinstance(entity, dict):
            return None
        tags = ModuleInterface._quality_labels_from_entity(entity)
        if not any("atmos" in str(t).lower() for t in tags):
            return None
        for label in ModuleInterface._amazon_catalog_quality_display_labels(tags):
            if "atmos" in str(label).lower() or "◗" in str(label):
                return label
        return None

    def _amazon_search_track_quality_labels(
        self,
        mobile_session: AmazonMusicMobileAPI,
        track_asin: str,
        media_region: AmazonRegion,
    ) -> list[str]:
        """One-track manifest probe for accurate track search Additional column."""
        if not track_asin:
            return []
        try:
            quality_tier = getattr(self.options, "quality_tier", None) or QualityEnum.HIGH
            spatial_labels = self._probe_quality_labels(
                mobile_session, (str(track_asin),), media_region, True, quality_tier
            )
            if spatial_labels:
                spatial_label = self._pick_spatial_display_label(
                    spatial_labels
                ) or next(iter(spatial_labels))
                return [self._amazon_manifest_label_for_display(spatial_label)]
            stereo_labels = self._probe_quality_labels(
                mobile_session, (str(track_asin),), media_region, False, quality_tier
            )
            if not stereo_labels:
                return []
            best = self._best_quality_display(stereo_labels)
            if not best:
                return []
            return [self._amazon_manifest_label_for_display(best)]
        except Exception as ex:
            LOGGER.debug("Track search manifest quality probe failed for %s: %s", track_asin, ex)
            return []

    @staticmethod
    def _playlist_payload_is_complete(p_data: dict) -> bool:
        """True when payload is a full playlist API response, not a text-search hit."""
        if not p_data or not isinstance(p_data, dict):
            return False
        meta = p_data.get("metadata")
        if not isinstance(meta, dict) or not meta.get("title"):
            return False
        tracks = p_data.get("tracks")
        if not tracks or not isinstance(tracks, list):
            return True
        first = tracks[0]
        if not isinstance(first, dict):
            return False
        return bool(first.get("metadata") or first.get("asin"))

    @staticmethod
    def _playlist_track_asin(track: dict) -> typing.Optional[str]:
        if not isinstance(track, dict):
            return None
        meta = track.get("metadata")
        if isinstance(meta, dict) and meta.get("asin"):
            return str(meta["asin"])
        if track.get("asin"):
            return str(track["asin"])
        return None

    @staticmethod
    def _playlist_track_metadata(track: dict) -> dict:
        if not isinstance(track, dict):
            return {}
        meta = track.get("metadata")
        return meta if isinstance(meta, dict) else track

    def get_album_info(self, album_id: str, media_region: typing.Optional[AmazonRegion] = None, data: dict = {}) -> Optional[AlbumInfo]:
        LOGGER.debug("Getting album info")
        if not media_region:
            split_album_id = album_id.split("_", maxsplit=1)
            if len(split_album_id[-1]) == 2:
                media_region = AmazonRegion.get_region_by_country(split_album_id[-1])
                album_id = split_album_id[0]
                
        if not media_region:
            raise TypeError(f"Invalid selected media region for {album_id}")
        
        mobile_session, _ = self.select_session(media_region)

        cached = data.get(album_id) if data and album_id in data else None
        # Text-search hits are not full albums (no tracks / nested artist); always fetch catalog album.
        if cached and isinstance(cached.get("tracks"), list) and cached["tracks"]:
            album_data = dict(cached)
        else:
            album_data = dict(
                mobile_session.get_album_info(
                    album_id,
                    use_alternative_naming=False,
                    region_to_use=media_region,
                )
            )
        # Force use the ASIN the API returns with
        album_id = album_data["asin"]
        
        valid_asins = [album_id]
        
        if asin := album_data.get("requestedAsin"):
            valid_asins.append(asin)
        if asin := album_data.get("globalAsin"):
            valid_asins.append(asin)
        
        valid_asins.extend((track["asin"] for track in album_data.get("tracks", [])))

        artist_name = self._metadata_artist_name(album_data)
        album_title = str(album_data.get("title") or "Unknown")
        cover_url, search_data = self.get_hi_res_cover(
            asins=valid_asins,
            query=f'"{artist_name}" - "{album_title}"',
            search_data=data.get(f"{album_id}_search") if data else None,
            mobile_session=mobile_session,
            media_region=media_region
        )
        if not cover_url:
            # Default 600 x 600 cover art
            cover_url = str(album_data.get("image", ""))
            tracks_cover_art = cover_url
        else:
            tracks_cover_art = self.format_cover_url(
                cover_url, self.options.default_cover_options
            )

        # album_data = self.mobile_session.get_album_info(album_id, use_alternative_naming=True)
        # pprint.pprint(mobile_session.get_page(f"album/{album_id}", region_to_use=media_region))

        mapped_tracks = {
            f"{asin}_quality_mapping": self.mpd_to_quality_map(mpd, asin, mobile_session.credentials.tier, media_region, mobile_session.credentials.account_region)
            for asin, mpd in mobile_session.get_tracks_manifest(
                (track["asin"] for track in album_data.get("tracks", [])),
                force_3d=any(
                    not self.settings["force_non_spatial"] or self.settings[key]
                    for key in ("prefer_spatial_mha1", "prefer_spatial_ac4")
                ),
                region_to_use=media_region
            )
            if mpd
        }

        per_track_audio = [
            self._get_usable_audio_track_of_mapped_quailty(
                mapped_audio_tracks, self.options.quality_tier
            )
            for mapped_audio_tracks in mapped_tracks.values()
        ]
        best_audio_track = max(per_track_audio, key=lambda i: i.bitrate, default=None)
        if not best_audio_track:
            raise TypeError("No available tracks.")
        track_quality_labels = {
            self._quality_label_from_mapped(mapped_audio_tracks)
            for mapped_audio_tracks in mapped_tracks.values()
        }
        album_quality_summary = self._best_quality_display(track_quality_labels)
        track_extra_kwargs = (
            {track["asin"]: track for track in album_data.get("tracks", [])}
            | {album_id: album_data}
            | mapped_tracks
            # | {f"{album_id}_page_entity_data": mobile_session.get_page(f"album/{album_id}", count=0, locale="en_US").get("entity", {})}
        )

        if search_data:
            track_extra_kwargs.update({f"{album_id}_search": search_data})
        else:
            self.print(
                (
                    f"Couldn't retrieve the search results for {album_data['title']}; {album_id}."
                    f" Is this ASIN in the same region as the account?"
                )
            )
        explicit = any(
            (track.get("parentalControls") or {}).get("hasExplicitLanguage")
            for track in album_data.get("tracks", [])
        )
        
        quality_tags = {
            "official_quality_name": best_audio_track.official_quality_name,
            "codec_pretty_name": codec_data[best_audio_track.codec].pretty_name,
            "bit_depth": best_audio_track.bit_depth,
            "sample_rate": best_audio_track.sample_rate,
        }

        return AlbumInfo(
            name=self.sanitize_parental_status_name(album_data.get("title", "Unknown")),
            artist=album_data.get("primaryArtistName", ""),
            tracks=[track["asin"] for track in album_data.get("tracks", [])],
            release_year=int(self._get_date_from_metadata(album_data).strftime("%Y")),
            explicit=explicit,
            duration=album_data.get("duration"),
            artist_id=album_data.get("artist", {}).get("asin"),  # optional
            # booklet_url="",  # optional
            cover_url=cover_url,  # optional
            cover_type=ImageFileTypeEnum.jpg,  # optional
            all_track_cover_jpg_url=tracks_cover_art,  # technically optional, but HIGHLY recommended
            animated_cover_url="",  # optional
            description="",  # optional
            quality=album_quality_summary
            or self._format_quality_display(
                quality_tags, str(self.settings.get("quality_format", ""))
            ),
            expected_track_count=int(album_data["trackCount"]) if album_data.get("trackCount") is not None else None,
            track_extra_kwargs={
                "data": track_extra_kwargs,
                "media_region": media_region,
            },  # optional, whatever you want
        )

    def get_playlist_info(
        self, playlist_id: str, media_region: AmazonRegion, data={}
    ) -> (
        PlaylistInfo
    ):  # Mandatory if either ModuleModes.download or ModuleModes.playlist
        mobile_session, _ = self.select_session(media_region)

        cached = data.get(playlist_id) if data and playlist_id in data else None
        if cached and self._playlist_payload_is_complete(cached):
            p_data = dict(cached)
        else:
            p_data = {}

        if not p_data:
            if len(playlist_id) == 10:
                # An ASIN (catalog playlist, e.g. Top 100)
                catalog = mobile_session.get_catalog_playlist(playlist_id, region_to_use=media_region)
                p_data = dict(catalog.get("playlist") or catalog)
            else:
                # A user playlist (either from the shared link, or the address bar)
                user_resp = mobile_session.get_user_playlist(playlist_id)
                playlists = user_resp.get("playlists") or []
                if not playlists:
                    raise TypeError(f"Playlist not found: {playlist_id}")
                p_data = dict(playlists[0])

        meta = p_data.get("metadata") if isinstance(p_data.get("metadata"), dict) else {}
        if not meta.get("title"):
            meta = {
                "title": p_data.get("title") or p_data.get("name") or "Unknown Playlist",
                "curatedBy": p_data.get("curatedBy") or p_data.get("artistName") or "",
                "durationSeconds": p_data.get("duration") or p_data.get("durationSeconds") or 0,
                "parentalControls": p_data.get("parentalControls") or {},
                "profileId": p_data.get("profileId"),
                "fourSquareArt": p_data.get("fourSquareArt") or p_data.get("artOriginal") or {},
                "description": p_data.get("description"),
            }
        cover_art = meta.get("fourSquareArt") or {}
        cover_url = ""
        if isinstance(cover_art, dict):
            cover_url = str(cover_art.get("url") or "")
        if not cover_url:
            art = p_data.get("artOriginal") or {}
            if isinstance(art, dict) and art.get("artUrl"):
                cover_url = str(art["artUrl"])

        track_asins = []
        track_data = {}
        for track in p_data.get("tracks", []) or []:
            asin = self._playlist_track_asin(track)
            if not asin:
                continue
            track_asins.append(asin)
            track_data[f"{asin}_playlist"] = self._playlist_track_metadata(track)

        playlist_year = self._entity_release_year(
            p_data, p_data, DownloadTypeEnum.playlist
        )

        return PlaylistInfo(
            name=str(meta.get("title") or "Unknown Playlist"),
            creator=str(meta.get("curatedBy") or ""),
            tracks=track_asins,
            release_year=int(playlist_year) if playlist_year else 0,
            duration=int(meta.get("durationSeconds") or 0),
            explicit=bool(
                (meta.get("parentalControls") or {}).get("hasExplicitLanguage", False)
            ),
            creator_id=meta.get("profileId"),
            cover_url=cover_url or None,
            cover_type=ImageFileTypeEnum.jpg,
            description=meta.get("description"),
            track_extra_kwargs={
                "media_region": media_region,
                "data": track_data,
            },
        )

    def get_artist_info(
        self, artist_id: str, get_credited_albums: bool, media_region: AmazonRegion, data: dict = {}
    ) -> ArtistInfo:  # Mandatory if ModuleModes.download
        mobile_session, _ = self.select_session(media_region)

        # get_credited_albums means stuff like remix compilations the artist was part of
        try:
            artist_data = mobile_session.get_artist_info(artist_id, region_to_use=media_region)
        except Exception as e:
            LOGGER.error(e, exc_info=True)
            raise e
        else:
            artist_id = str(artist_data["asin"])
            artist_name = str(artist_data["name"])

        albums: list[dict[str, typing.Any]] = []
        albums_metadata: list[dict[str, typing.Any]] = []

        self.print(
            f"{module_information.service_name}: Loading albums for {artist_data['name']}, this may take a while.."
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(
                    mobile_session.get_album_info, album["asin"], region_to_use=media_region
                ): album
                for album in mobile_session.search(
                    query=f'"{artist_name}"',
                    search_types=("catalog_album",),
                    limit=100,
                    region_to_use=media_region
                )
                if artist_name in album.get("artistName", "")
            }
            executor.shutdown(wait=True)
            for future in concurrent.futures.as_completed(futures):
                album_metadata = future.result()
                if not album_metadata:
                    continue
                album = futures[future]
                # Filter out foreign albums with the same artist name (but not ID)
                if (
                    not album_metadata["artist"]["contributorAsins"]
                    and album_metadata["artist"]["asin"] != artist_id
                ):
                    continue

                # Assume that if they aren't equal, they are credited albums
                # artist_id != album.get("artistAsin")
                if (
                    artist_name != album.get("artistName", "")
                ) and not get_credited_albums:
                    continue

                albums_metadata.append(album_metadata)
                albums.append(album)

        if not albums:
            self.print(f"No albums found for {artist_name} with ID: {artist_id}.")
            return ArtistInfo(artist_name)

        return ArtistInfo(
            name=artist_name,
            albums=[album["asin"] for album in albums],
            album_extra_kwargs={
                "media_region": media_region,
                "data": {
                    f"{album_data['asin']}_search": album_data for album_data in albums
                }
                | {
                    str(album_meta["asin"]): album_meta
                    for album_meta in albums_metadata
                }
            },
            # tracks=[],
            # track_extra_kwargs={"data": ""},  # optional, whatever you want
        )

    def get_track_credits(
        self, track_id: str, media_region: AmazonRegion, data={}, **kwargs
    ):  # Mandatory if ModuleModes.credits
        mobile_session, _ = self.select_session(media_region)
        track_credits = mobile_session.get_track_xray(track_id, region_to_use=media_region, parse_credits=True)
        return [
            CreditsInfo(
                sanitise_name(k),
                list(
                    itertools.chain.from_iterable(
                        [self.parse_credit_names_from_name(name) for name in v]
                    )
                ),
            )
            for k, v in track_credits.items()
        ]

    def get_track_cover(
        self, track_id: str, cover_options: CoverOptions, media_region: AmazonRegion, data={}, **kwargs
    ) -> CoverInfo:  # Mandatory if ModuleModes.covers
        mobile_session, _ = self.select_session(media_region)
        
        track_data = (
            data[track_id]
            if track_id in data
            else mobile_session.get_track_info(track_id, region_to_use=media_region) #music_territory="CY"
        )
        album_id = str(track_data["album"]["asin"])
        album_data = (
            data[track_id]
            if track_id in data
            else mobile_session.get_album_info(album_id, region_to_use=media_region) #music_territory="CY"
        )

        valid_album_asins = [album_id]
        
        if asin := album_data.get("requestedAsin"):
            valid_album_asins.append(asin)
        if asin := album_data.get("globalAsin"):
            valid_album_asins.append(asin)

        cover_url, _ = self.get_hi_res_cover(
            asins=valid_album_asins,
            query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
            search_data=data.get(f"{album_id}_search"),
            mobile_session=mobile_session,
            media_region=media_region
        )
        if not cover_url:
            # Default 600 x 600 cover art
            cover_url = str(album_data.get("image", ""))
        else:
            cover_url = self.format_cover_url(
                cover_url, cover_options
            )

        return CoverInfo(
            url=cover_url,
            file_type=cover_options.file_type,
        )

    def get_track_lyrics(
        self, track_id: str, media_region: AmazonRegion, data={}, **kwargs
    ) -> LyricsInfo:  # Mandatory if ModuleModes.lyrics
        mobile_session, _ = self.select_session(media_region)
        track_lyrics_resp = mobile_session.get_track_lyrics(track_id, region_to_use=media_region) # music_territory="CY"

        embedded_lyrics = ""
        synced_lyrics = ""
        if int(track_lyrics_resp.get("lyricsResponseCode", 0)) == 1002:
            for line in track_lyrics_resp.get("lyrics", {}).get("lines", {}):
                text = line["text"]

                start_time = int(line["startTime"])
                start_time_str = self.milliseconds_to_lrc_time(start_time)

                embedded_lyrics += f"[{start_time_str}] {text}\n"
                synced_lyrics += f"[{start_time_str}]{text}\n"

        return LyricsInfo(
            embedded=embedded_lyrics, synced=synced_lyrics
        )  # both optional if not found

    def _search_additional_quality_labels(
        self,
        mobile_session: AmazonMusicMobileAPI,
        catalog: dict,
        media_region: AmazonRegion,
        query_type: DownloadTypeEnum,
        max_track_probe: int = SEARCH_QUALITY_MAX_TRACK_PROBE,
    ) -> typing.Optional[list[str]]:
        """
        Quality from track MPD manifests (same source as expand/download).
        Search/artist rows sample a few tracks per album for speed; expand still uses all tracks.
        Returns None to fall back to contentEncoding flags when the probe fails.
        """
        track_asins: list[str] = []
        if query_type == DownloadTypeEnum.album:
            for track in catalog.get("tracks") or []:
                if isinstance(track, dict) and track.get("asin"):
                    track_asins.append(str(track["asin"]))
        elif query_type == DownloadTypeEnum.playlist:
            for track in catalog.get("tracks") or []:
                asin = self._playlist_track_asin(track)
                if asin:
                    track_asins.append(str(asin))
        if not track_asins:
            return None
        probe_asins = self._sample_track_asins_for_quality_probe(
            track_asins, max_probe=max_track_probe
        )
        try:
            quality_tier = getattr(self.options, "quality_tier", None) or QualityEnum.HIGH
            # Fast path: sample a few stereo/lossless/opus manifests.
            stereo_labels = self._probe_quality_labels(
                mobile_session, probe_asins, media_region, False, quality_tier
            )
            # Always probe spatial on the first track (one extra call) — pure Atmos albums
            # only expose 3D/Dolby tiers when force_3d is enabled.
            spatial_labels: set[str] = set()
            if probe_asins:
                spatial_labels = self._probe_quality_labels(
                    mobile_session,
                    (probe_asins[0],),
                    media_region,
                    True,
                    quality_tier,
                )
            if spatial_labels:
                spatial_label = self._pick_spatial_display_label(
                    spatial_labels
                ) or next(iter(spatial_labels))
                return [self._amazon_manifest_label_for_display(spatial_label)]
            if not stereo_labels:
                return None
            best = self._best_quality_display(stereo_labels)
            if not best:
                return None
            return [self._amazon_manifest_label_for_display(best)]
        except Exception as ex:
            LOGGER.debug("Search manifest quality probe failed: %s", ex)
            return None

    def artist_album_display_meta(
        self, album_catalog: dict, media_region: AmazonRegion
    ) -> dict[str, typing.Any]:
        """Year, track count, and best quality for artist discography rows (matches album search)."""
        year = ""
        try:
            if album_catalog.get("originalReleaseDate") or album_catalog.get("merchantReleaseDate"):
                year = self._get_date_from_metadata(
                    {
                        "originalReleaseDate": album_catalog.get("originalReleaseDate"),
                        "merchantReleaseDate": album_catalog.get("merchantReleaseDate"),
                    }
                ).strftime("%Y")
        except (OSError, OverflowError, ValueError, TypeError):
            pass

        additional_parts: list[str] = []
        track_count = album_catalog.get("trackCount")
        if track_count is None and isinstance(album_catalog.get("tracks"), list):
            track_count = len(album_catalog["tracks"])
        if track_count is not None:
            try:
                tc = int(track_count)
                if tc > 0:
                    additional_parts.append("1 track" if tc == 1 else f"{tc} tracks")
            except (TypeError, ValueError):
                pass

        try:
            mobile_session, _ = self.select_session(media_region)
            quality_labels = self._search_additional_quality_labels(
                mobile_session, album_catalog, media_region, DownloadTypeEnum.album
            )
            if quality_labels:
                additional_parts.extend(quality_labels)
        except Exception as ex:
            LOGGER.debug("Artist album quality probe failed: %s", ex)

        title = str(album_catalog.get("title") or "")
        explicit = any(
            (track.get("parentalControls") or {}).get("hasExplicitLanguage")
            for track in (album_catalog.get("tracks") or [])
            if isinstance(track, dict)
        ) or "[explicit]" in title.lower()

        return {
            "year": year,
            "additional": " / ".join(additional_parts),
            "explicit": explicit,
        }

    def search(
        self,
        query_type: DownloadTypeEnum,
        query: str,
        track_info: typing.Optional[TrackInfo] = None,
        limit: int = 10,
    ):  # Mandatory
        preferred_region = (
            self.select_usable_region_from_regions(
                AmazonRegion.get_available_regions_by_continent(
                    self.settings["prefer_account_continent"]
                )
            )
            or AmazonRegion.get_available_regions_by_continent(
                self.settings["prefer_account_continent"]
            )[0]
        )
        mobile_session, media_region = self.select_session(preferred_region)
        self.print(f"{module_information.service_name}: Using {media_region.country} for search query ({query})")

        results = []
        search_type = f"catalog_{query_type.name}"
        # if track_info and track_info.tags.isrc:
        #     results = list(
        #         mobile_session.search(
        #             query=track_info.tags.isrc, search_types=(search_type,), limit=limit
        #         )
        #     )
        if not results:
            results = list(
                mobile_session.search(
                    query=query,
                    search_types=(search_type,),
                    limit=limit,
                    region_to_use=media_region
                )
            )

        def _cover_from_item(item: dict) -> typing.Optional[str]:
            art = item.get("artOriginal") or {}
            url = art.get("artUrl") if isinstance(art, dict) else None
            if url:
                return str(url)
            album_art = (item.get("metadata") or {}).get("albumArt") or {}
            if isinstance(album_art, dict) and album_art.get("url"):
                return str(album_art["url"])
            return None

        def _fetch_catalog_entity_for_search(item: dict) -> typing.Optional[dict]:
            """Full album/playlist metadata (text search hits omit year, duration, quality)."""
            asin = item.get("asin") or item.get("seriesAsin")
            if not asin or query_type not in (DownloadTypeEnum.album, DownloadTypeEnum.playlist):
                return None
            try:
                if query_type == DownloadTypeEnum.playlist:
                    if len(str(asin)) == 10:
                        resp = mobile_session.get_catalog_playlist(
                            str(asin), region_to_use=media_region
                        )
                        return dict(resp.get("playlist") or resp)
                    user_resp = mobile_session.get_user_playlist(str(asin))
                    playlists = user_resp.get("playlists") or []
                    if playlists:
                        return dict(playlists[0])
                elif query_type == DownloadTypeEnum.album:
                    return dict(
                        mobile_session.get_album_info(str(asin), region_to_use=media_region)
                    )
            except Exception as ex:
                LOGGER.debug("Catalog metadata fetch failed for %s: %s", asin, ex)
            return None

        def _search_release_year(
            item: dict, catalog: typing.Optional[dict] = None
        ) -> typing.Optional[str]:
            return self._entity_release_year(item, catalog, query_type)

        def _search_duration_seconds(
            item: dict, catalog: typing.Optional[dict] = None
        ) -> typing.Optional[int]:
            for src in (catalog, item):
                if not isinstance(src, dict):
                    continue
                raw = src.get("duration")
                if raw is None and isinstance(src.get("metadata"), dict):
                    raw = src["metadata"].get("durationSeconds")
                if raw is None:
                    continue
                try:
                    sec = int(raw)
                    if sec > 86400:
                        sec = sec // 1000
                    return sec if sec >= 0 else None
                except (TypeError, ValueError):
                    continue
            return None

        def _quality_for_search_item(item: dict, catalog: typing.Optional[dict] = None) -> list[str]:
            if catalog and query_type in (
                DownloadTypeEnum.album,
                DownloadTypeEnum.playlist,
            ):
                manifest_labels = self._search_additional_quality_labels(
                    mobile_session, catalog, media_region, query_type
                )
                if manifest_labels is not None:
                    if not self._quality_labels_include_atmos(manifest_labels):
                        atmos = self._catalog_atmos_display_label(catalog) or self._catalog_atmos_display_label(item)
                        if atmos:
                            return [atmos]
                    return manifest_labels
            if query_type == DownloadTypeEnum.track:
                track_asin = item.get("asin")
                if track_asin:
                    probed = self._amazon_search_track_quality_labels(
                        mobile_session, str(track_asin), media_region
                    )
                    if probed:
                        if not self._quality_labels_include_atmos(probed):
                            atmos = self._catalog_atmos_display_label(item) or (
                                self._catalog_atmos_display_label(catalog) if catalog else None
                            )
                            if atmos:
                                return [atmos]
                        return probed
            labels = self._quality_labels_from_entity(item)
            if not labels and catalog:
                labels = self._quality_labels_from_entity(catalog)
            if labels:
                return self._amazon_catalog_quality_display_labels(labels)
            return labels

        def _build_search_result(item: dict) -> SearchResult:
            asin = item.get("asin") or item.get("seriesAsin")
            catalog = _fetch_catalog_entity_for_search(item) if asin else None
            data_payload = {f"{asin}_search": item} if asin else {}
            if catalog and asin:
                data_payload[str(asin)] = catalog
            if query_type == DownloadTypeEnum.playlist:
                artists: typing.Optional[list[str]] = ["Amazon Music"]
            else:
                artists = [item["artistName"]] if item.get("artistName") else None
            return SearchResult(
                result_id=asin,
                name=item.get("title") or item.get("name"),
                artists=artists,
                year=_search_release_year(item, catalog)
                if query_type
                in (
                    DownloadTypeEnum.track,
                    DownloadTypeEnum.album,
                    DownloadTypeEnum.playlist,
                )
                else None,
                duration=_search_duration_seconds(item, catalog),
                image_url=_cover_from_item(item),
                explicit=item["parentalControls"]["hasExplicitLanguage"]
                if item.get("parentalControls")
                else None,
                additional=_quality_for_search_item(item, catalog),
                extra_kwargs={
                    "media_region": media_region,
                    "data": data_payload,
                },
            )

        if query_type in (DownloadTypeEnum.album, DownloadTypeEnum.playlist) and len(results) > 1:
            search_output: list[SearchResult] = []
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, len(results))
            ) as executor:
                for result in executor.map(_build_search_result, results):
                    search_output.append(result)
            return search_output

        return [_build_search_result(item) for item in results]

    # helpers

    def get_hi_res_cover(
        self,
        asins: typing.Iterable[str],
        query: str,
        mobile_session: AmazonMusicMobileAPI,
        media_region: AmazonRegion,
        search_data: typing.Optional[dict] = {},
    ):
        search_data = {}
        cover_url = None

        # Typically this works
        if not search_data:
            search_data = mobile_session.search(
                query=query,
                asins=tuple(asins),
                search_types=("catalog_album", "catalog_track"),
                limit=100,
                region_to_use=media_region
            )

        cover_url = str(
            search_data.get("artOriginal", {}).get(
                "artUrl",
                search_data.get("metadata", {}).get("albumArt", {}).get("url", ""),
            )
        )

        if not cover_url:
            # These types of album/tracks are unique as they only appear in Playlists (curated by Amazon Music)
            # e.g tracks with the label "Amazon Content Service"
            # We have to trust that Amazon Search API returns the correct playlist that has the album/track inside of it
            for p_search_data in mobile_session.search(
                # query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
                query=query,
                search_types=("catalog_playlist",),
                limit=100,
                region_to_use=media_region
            ):
                p_data = mobile_session.get_catalog_playlist(
                    p_search_data["seriesAsin"], region_to_use=media_region
                )
                for track in p_data.get("playlist", {}).get("tracks", []):
                    if track.get("metadata", {}).get("albumAsin") not in asins:
                        continue
                    search_data = track
                    cover_url = str(
                        track.get("metadata", {}).get("albumArt", {}).get("url", "")
                    )
                    break
                else:
                    continue
                break

        return cover_url, search_data

    _ALBUM_YEAR_KEYS = (
        "originalReleaseDate",
        "merchantReleaseDate",
        "releaseDate",
        "albumReleaseDate",
    )
    _PLAYLIST_YEAR_KEYS = (
        "lastUpdatedDate",
        "creationDate",
        "lastModifiedDate",
        "publishedDate",
        "publishDate",
        "updatedDate",
        "createdDate",
        "uploadDate",
        "playlistReleaseDate",
        "releaseYear",
        "releaseDate",
        "originalReleaseDate",
        "merchantReleaseDate",
    )

    @classmethod
    def _parse_amazon_year(cls, raw: typing.Any) -> typing.Optional[str]:
        """Parse Amazon ms/second timestamps, year ints, or date strings to YYYY."""
        if raw is None:
            return None
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return None
            if s.isdigit():
                raw = int(s)
            elif len(s) >= 4 and s[:4].isdigit():
                return s[:4]
            else:
                match = re.search(r"\b(19|20)\d{2}\b", s)
                return match.group(0) if match else None
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None
        if 1000 <= val <= 9999:
            return str(val)
        try:
            ms = val if val > 1_000_000_000_000 else val * 1000
            return cls._get_date_from_metadata({"originalReleaseDate": ms}).strftime("%Y")
        except (OSError, OverflowError, ValueError, TypeError):
            return None

    @classmethod
    def _year_from_date_fields(
        cls, entity: typing.Optional[dict], date_keys: tuple[str, ...]
    ) -> typing.Optional[str]:
        if not isinstance(entity, dict):
            return None
        for key in date_keys:
            year = cls._parse_amazon_year(entity.get(key))
            if year:
                return year
        meta = entity.get("metadata")
        if isinstance(meta, dict) and meta is not entity:
            return cls._year_from_date_fields(meta, date_keys)
        return None

    @classmethod
    def _playlist_year_from_track_metadata(
        cls, catalog: dict, max_tracks: int = 25
    ) -> typing.Optional[str]:
        """User/catalog playlists: lastUpdatedDate or creationDate on track rows."""
        years: list[int] = []
        for track in (catalog.get("tracks") or [])[:max_tracks]:
            if not isinstance(track, dict):
                continue
            meta = track.get("metadata")
            if not isinstance(meta, dict):
                meta = track
            for key in ("lastUpdatedDate", "creationDate"):
                year = cls._parse_amazon_year(meta.get(key))
                if year:
                    try:
                        years.append(int(year))
                    except ValueError:
                        pass
        return str(max(years)) if years else None

    def _entity_release_year(
        self,
        item: dict,
        catalog: typing.Optional[dict],
        query_type: DownloadTypeEnum,
    ) -> typing.Optional[str]:
        keys = (
            self._PLAYLIST_YEAR_KEYS
            if query_type == DownloadTypeEnum.playlist
            else self._ALBUM_YEAR_KEYS
        )
        for src in (catalog, item):
            year = self._year_from_date_fields(src, keys)
            if year:
                return year
        if query_type == DownloadTypeEnum.playlist and catalog:
            return self._playlist_year_from_track_metadata(catalog)
        return None

    @staticmethod
    def _get_date_from_metadata(album_data: dict[str, typing.Any]):
        proper_ts = (
            int(
                str(
                    album_data.get("originalReleaseDate")
                    or album_data.get("merchantReleaseDate")
                )
            )
            / 1000
        )
        if os.name == "nt" and proper_ts < 0:
            # Windows doesn't fully support any dates
            # before 1970 (negative dates), so this is a workaround.
            return datetime(1970, 1, 1) - timedelta(seconds=abs(proper_ts))

        return datetime.fromtimestamp(proper_ts)

    @staticmethod
    def milliseconds_to_lrc_time(milliseconds: int):
        # Convert milliseconds to the proper LRC time format [mm:ss.xxx]
        return f"{milliseconds // 60000:02}:{(milliseconds // 1000) % 60:02}.{milliseconds % 1000:03}"

    @staticmethod
    def sanitize_parental_status_name(name: str):
        sanitized = re.split(r"(\s\[Explicit\]|\s\[Clean\])$", name, maxsplit=1)
        if len(sanitized) >= 2:
            return sanitized[0]
        # No matches found
        return name

    @staticmethod
    def parse_credit_names_from_name(name: str):
        return list(
            {str(item) for item in re.split(r" & |, | - | / | feat. ", name) if item}
        )

    @staticmethod
    def parse_genres_from_genre(name: str):
        return {
            str(item).title()
            for item in re.split(r", | & | - | / |/", name)
            if item != "General"
        }

    @staticmethod
    def format_cover_url(url: str, options: CoverOptions):
        frag_url, extension = url.rsplit(".", 1)
        file_format = (
            f"_FM{options.file_type.name}"
            if options.file_type != ImageFileTypeEnum.jpg
            else ""
        )

        new_url = f"{frag_url}.SX{options.resolution}{file_format}.{extension}"

        return new_url

    def download(self, url: str, location: str, use_aria2c: typing.Optional[bool]):
        # OrpheusDL default downloader only (aria2c/aria2p not supported in this distribution)
        indent = getattr(self.module_controller.printer_controller, "indent_number", 0)
        download_file(
            url,
            location,
            enable_progress_bar=bool(self.module_controller.progress_bar_enabled),
            indent_level=indent,
        )

    def _get_usable_audio_track_of_mapped_quailty(
        self,
        mapped_audio_tracks: dict[QualityEnum, dict[str, list[AudioTrack]]],
        quality_tier: QualityEnum,
        to_print: typing.Optional[bool] = False,
    ):
        track_to_use: AudioTrack | None = None
        # I tried, ok?
        # Attempt to find and choose preferred quality (has to be the same returned in the MPD)
        mapped_qual_tracks = list(
            self._iter_over_tracks_to_quality_map(mapped_audio_tracks)
        )

        if max_track_quality_to_use := self.settings["max_track_quality_to_use"]:
            max_track_quality_to_use = str(max_track_quality_to_use).upper()
            for (
                quality_enum,
                quality_name,
                tracks,
            ) in mapped_qual_tracks:
                if not tracks:
                    continue

                # Check if the selected MPD quality to use is valid
                if max_track_quality_to_use not in quality_name:
                    continue

                if to_print:
                    self.print(
                        f"{module_information.service_name}: Downloading in {quality_name} as it is matched with {max_track_quality_to_use}."
                    )

                for track in tracks:
                    if not (track.quality.startswith(max_track_quality_to_use)):
                        continue
                    track_to_use = track
                    break
                else:
                    continue
                break

            if not track_to_use and to_print:
                self.print(
                    f"{module_information.service_name}: Failed to find {max_track_quality_to_use!r}, defaulting to highest avaliable."
                )

        # Handle 360RA files seperately because Amazon ranks 360RA differently
        if not track_to_use:
            if item := list(i for i in mapped_qual_tracks if "RA360" in i[1]):
                # Get the last item (max quality) list of tuple and get the first item inside tracks
                track_to_use = item[-1][-1][0]

        # If max_track_quality_to_use is not set or failed, then use the global quality to use
        if not track_to_use:
            for (
                quality_enum,
                quality_name,
                tracks,
            ) in mapped_qual_tracks:
                # self.print(f"{quality_enum=}, {quality_name=}, {tracks=}")
                # pprint.pprint(mapped_qual_tracks)
                if not tracks:
                    continue

                # tracks are sorted highest-bitrate first; tracks[0] is always chosen below.
                # Skip the multi-tier notice when we already take the best manifest tier.
                if len(tracks) > 1 and to_print:
                    best_bitrate = max(t.bitrate for t in tracks)
                    if tracks[0].bitrate < best_bitrate:
                        self.print(
                            (
                                f"{module_information.service_name}: "
                                f"There are more than one tracks avaliable for {quality_name}. "
                                f"\nAvaliable qualities: "
                            )
                            + (
                                "".join(
                                    f"{module_information.service_name}: {item.quality} with ranking {tracks.index(item) + 1}, "
                                    for item in tracks
                                )
                            )
                        )

                if quality_enum.value <= quality_tier.value:
                    track_to_use = tracks[0]
                    break

                continue
        

        if not isinstance(track_to_use, AudioTrack):
            raise TypeError(f"{track_to_use=}, type: {type(track_to_use)}")

        LOGGER.debug(f"Using AudioTrack: {track_to_use}")
        return track_to_use

    def mpd_to_quality_map(
        self,
        mpd: ElementTree.Element,
        track_asin: str,
        subscription_tier: AmazonMusicTier,
        media_region: AmazonRegion,
        account_region: AmazonRegion
    ):
        """
        A helper function to retrieve a mapping of a QualityEnum
        to a dictionary of a Quality and a list of AudioTracks via a MPD.

        """
        # LOGGER.debug(json.dumps(mpd, indent=3))

        avaliable_tracks = self.get_audios_from_mpd(mpd, track_asin)

        # Sorting by bitrate helps retrieve the highest resolution avaliable
        LOGGER.debug(avaliable_tracks)

        preferred_codecs = [CodecEnum.OPUS]
        # TS: 1703471476
        # I fucking wrote the entitlement and KATANA exclusion condition in the wrong place
        # I'll write it later, but TODO it's supposed to be its own independant function after mapped tracks

        # Know what entitlements are avaliable
        available_entitlements = set(
            i
            for track in avaliable_tracks
            for i in track.entitlements.entitlement_names
        )
        has_entitlements = bool(
            self.settings["use_own_master_keys"]
            # Make sure the key required exists
            and any(
                f"KATANA:{media_region.country}" in str(item)
                for item in self.settings["master_keys"]
            )
            # Make sure the entitlement PSSH exists
            and any(
                local_name in av_entitlement_name
                for local_name, _ in (
                    item.split(":", maxsplit=1)
                    for item in self.settings["master_keys"]
                )
                for av_entitlement_name in available_entitlements
            )
        )
        has_katana_tier = (
            not self.settings["use_own_master_keys"]
            and (
                self.options.disable_subscription_check
                or subscription_tier is AmazonMusicTier.UNLIMITED
            )
            # Needed as license acquitsion, might be blocked due to cross region calling
            and account_region.region == media_region.region
        )

        # print(f"{has_entitlements=}, {has_katana_tier=}, {available_entitlements=}")
        if (
            has_entitlements or has_katana_tier
        ):
            # print("flac")
            preferred_codecs.append(CodecEnum.FLAC)

            if not self.settings["force_non_spatial"]:
                if self.settings["prefer_spatial_mha1"]:
                    preferred_codecs.append(CodecEnum.MHA1)
                else:
                    preferred_codecs.append(CodecEnum.MHM1)

                if self.settings["prefer_spatial_ac4"]:
                    preferred_codecs.append(CodecEnum.AC4)
                else:
                    preferred_codecs.append(CodecEnum.EAC3)
        
        mapped_audio_tracks = self.tracks_to_quality_map(
            avaliable_tracks, preferred_codecs, entitlements_only=has_entitlements, allow_web_pssh=has_katana_tier
        )
        LOGGER.debug(mapped_audio_tracks)
        return mapped_audio_tracks
    
    def get_decrypted_key(self, enc_key: bytes, pssh: PSSH, enc_key_type: str):
        pssh_data = WidevinePsshData()
        pssh_data.ParseFromString(pssh.init_data)
        entitled_key = pssh_data.entitled_keys[0]

        LOGGER.debug(f"Using {enc_key.hex()} as the master key.")
        LOGGER.debug(f"Raw encrypted key ID and key: {entitled_key.key_id.hex()}:{entitled_key.key.hex()}")
        key_id = entitled_key.key_id.hex()
        
        cipher = AES.new(enc_key, AES.MODE_CBC, entitled_key.iv)
        raw_final_key = cipher.decrypt(entitled_key.key)
        
        if enc_key_type == "ENTITLEMENT":
            final_key = raw_final_key.hex()
        elif enc_key_type == "CONTENT":
            final_key = raw_final_key[:16].hex()
        else:
            raise ValueError(f"enc_key_type must be ENTITLEMENT or CONTENT, not {enc_key_type}")
        
        LOGGER.debug(f"Raw key ({enc_key_type}): {raw_final_key.hex()}, IV: {entitled_key.iv.hex()}")

        return key_id, final_key
        

    def get_audios_from_mpd(self, manifest: ElementTree.Element, track_asin: str):
        """
        Retrieve each avaliable audio quality/format as a AudioTrack.

        The manifest must be retrieved from manifest API endpoint with the SIREN / SIREN_KATANA DASH version.

        The manifest must follow the `urn:mpeg:dash:profile:isoff-on-demand:2011` XML profile.
        """
        avaliable_tracks: list[AudioTrack] = []
        ns = {
            "mpd": "urn:mpeg:dash:schema:mpd:2011",
            "drm": "urn:mpeg:cenc:2013",
            "amz": "urn:amazon:music:drm:2019",
        }

        for adaptation_set in manifest.findall(".//AdaptationSet", ns):
            entitlement_psshs: dict[str, PSSH] = {}
            web_pssh: PSSH | None = None
            for content_protection in adaptation_set.findall(
                "ContentProtection", ns
            ):
                if (
                    content_protection.get("schemeIdUri")
                    == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
                    and content_protection.get("value") == "AmzMusic-2019"
                ):
                    entitlement_psshs.update(
                        {
                            content_protection.find("amz:groupId", ns).text.split("_")[0].lower(): PSSH(content_protection.find("drm:pssh", ns).text, strict=True)
                        }
                    )
                if content_protection.get(
                    "schemeIdUri"
                ) == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" and not content_protection.get(
                    "value"
                ):
                    web_pssh = PSSH(content_protection.find("drm:pssh", ns).text, strict=True)
                    
            if not web_pssh:
                LOGGER.warning("Failed to find the PSSH for web playback. License acquisition may fail.")
            
            official_quality_name = ""
            audio_ref_loudness = ""

            for prop in adaptation_set.findall("SupplementalProperty"):
                # Get track type property (LD, SD, HD, SD)
                if prop.get("schemeIdUri") == "amz-music:trackType":
                    official_quality_name = prop.get("value", "Unknown")
                    LOGGER.debug(
                        f"Official name for track: {official_quality_name}"
                    )
                    continue

                # Get track loudness property
                if prop.get("schemeIdUri") == "urn:mpeg:mpegB:cicp:ProgramLoudness":
                    audio_ref_loudness = prop.get("value", "Unknown")
                    continue

            for representation in adaptation_set.findall("Representation", ns):
                media_url_elem = representation.find("BaseURL", ns)
                if not (media_url_elem is not None and media_url_elem.text):
                    raise IndexError(f"Failed to find Media URL for {track_asin}")
                media_url = media_url_elem.text

                media_url_query = parse_qs(urlparse(media_url).query)
                quality = str(media_url_query.get("ql", [""])[0])

                if quality.startswith(
                    "UHD"
                ) and not official_quality_name.startswith("UHD"):
                    # amz-music:trackType only returns the following:
                    # LD, SD, HD, and 3D
                    official_quality_name = "UHD"
                elif not official_quality_name:
                    official_quality_name = quality

                codec = str(representation.get("codecs")).upper()
                # 360 Audio
                if codec.startswith("MHA1"):
                    codec = "MHA1"
                elif codec.startswith("MHM1"):
                    codec = "MHM1"
                # Dolby Atmos
                elif codec.startswith("EC-3"):
                    codec = "EAC3"
                elif codec.startswith("AC-4"):
                    codec = "AC4"
                # V2 musicDashVersionList
                elif codec.startswith("MP4A"):
                    codec = "AAC"

                codec = CodecEnum[codec]

                bit_depth = None
                sp = representation.find("SupplementalProperty")
                if sp is not None and not codec_data[codec].spatial:
                    bit_depth = int(sp.get("value", 0))

                avaliable_tracks.append(
                    AudioTrack(
                        asin=track_asin,
                        codec=codec,
                        bit_depth=bit_depth,
                        bitrate=int(
                            int(representation.get("bandwidth") or 0) // 1000
                        ),
                        sample_rate=float(
                            int(representation.get("audioSamplingRate") or 0) / 1000
                        ),
                        url=media_url,
                        official_quality_name=official_quality_name,
                        quality_ranking=int(
                            representation.get("qualityRanking", 0)
                        ),
                        quality=quality,
                        entitlements=PSSHEntitlements(**entitlement_psshs),
                        web_pssh=web_pssh,
                        reference_loudness=audio_ref_loudness,
                    )
                )
                
        if not avaliable_tracks:
            raise ValueError("No tracks found!")

        avaliable_tracks = natsort.natsorted(
            avaliable_tracks, key=lambda x: x.quality, reverse=False
        )
        # import pprint
        # pprint.pprint(avaliable_tracks)
        return avaliable_tracks

    @staticmethod
    def _iter_over_tracks_to_quality_map(
        mapping: dict[QualityEnum, OrderedDict[str, list[AudioTrack]]]
    ):
        for quality_enum in reversed(QualityEnum):
            quality_tracks = mapping.get(quality_enum)
            if not quality_tracks:
                continue
            for quality_name, tracks in quality_tracks.items():
                # print(f"{quality_enum=}, {quality_name=}, {tracks=}")
                yield quality_enum, quality_name, tracks

        return

    def tracks_to_quality_map(
        self,
        tracks: typing.Iterable[AudioTrack],
        preferred_codecs: typing.Iterable[CodecEnum],
        entitlements_only: typing.Optional[bool] = None,
        allow_web_pssh: typing.Optional[bool] = None,
    ) -> dict[QualityEnum, dict[str, list[AudioTrack]]]:
        """
        Example output:

        {
            "hifi": {
                "SPATIAL_ATMOS_HIGH": [
                    AudioTrack(),
                ],
                "SPATIAL_ATMOS_MEDIUM": [
                    AudioTrack()
                ]
            },
        }

        """
        quality_to_track_mapping = {}

        for quality_enum, qualities in reversed(self.quality_parse.items()):
            def key_for_sorting_avaliable_tracks(track: AudioTrack):
                return track.bitrate

            def key_for_filtering_audiotracks(track: AudioTrack):
                if allow_web_pssh and not track.web_pssh:
                    return
                elif entitlements_only and not track.entitlements.music_territory:
                    return
                for quality in qualities:
                    if not (
                        track.quality.startswith(quality)
                        and track.codec in preferred_codecs
                    ):
                        continue
                    return quality
                return

            def key_for_grouping_audiotracks(track: AudioTrack):
                return track.official_quality_name

            # AudioTracks are sorted best quality to worse
            
            grouped_tracks = OrderedDict([
                (key, natsort.natsorted(
                    group, key=key_for_sorting_avaliable_tracks, reverse=True
                ))
                for key, group in itertools.groupby(
                    [item for item in tracks if key_for_filtering_audiotracks(item)],
                    key=key_for_grouping_audiotracks,
                )
            ])

            quality_to_track_mapping.update({quality_enum: grouped_tracks})

        # import pprint
        # pprint.pprint(quality_to_track_mapping)

        return quality_to_track_mapping

    @staticmethod
    def _try_mp4decrypt(encrypted_file: str, destination_file: str, key_id: str, key: str) -> bool:
        """Fallback decrypt via Bento4 mp4decrypt when Shaka Packager crashes (e.g. Tiny10 VM)."""
        executable = resolve_mp4decrypt()
        if not executable:
            return False

        enc_path = os.path.abspath(encrypted_file)
        out_path = os.path.abspath(destination_file)
        env = get_clean_env()
        tool_dir = str(executable.parent)
        env["PATH"] = tool_dir + os.pathsep + env.get("PATH", "")

        # KID:key (CENC), then track index fallbacks per Bento4 docs.
        key_specs = (
            f"{key_id}:{key}",
            f"1:{key}",
            f"0:{key}",
        )
        for key_spec in key_specs:
            if os.path.isfile(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            run_kwargs = {
                "args": [str(executable), "--key", key_spec, enc_path, out_path],
                "env": env,
                "cwd": os.path.dirname(enc_path) or os.getcwd(),
                "capture_output": True,
                "text": True,
            }
            if sys.platform == "win32":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(**run_kwargs)
            if result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                LOGGER.info(
                    "mp4decrypt decrypted %s bytes (key spec %s)",
                    os.path.getsize(out_path),
                    key_spec.split(":")[0],
                )
                return True
            LOGGER.debug(
                "mp4decrypt failed (exit %s, key %s): %s",
                result.returncode,
                key_spec,
                ((result.stderr or "") + (result.stdout or "")).strip()[:300],
            )
        return False

    @staticmethod
    def call_shaka_packager(encrypted_file: str, destination_file: str, key_id: str, key: str, label: str):
        executable = resolve_shaka_packager()
        if not executable:
            raise EnvironmentError(
                "Shaka Packager executable not found but is required.\n"
                f"Download it at: {SHAKA_PACKAGER_HELP_URL}\n"
                "Place packager-win-x64.exe next to OrpheusDL_GUI.exe (or orpheus.py when running from source)."
            )

        enc_path = os.path.abspath(encrypted_file)
        out_path = os.path.abspath(destination_file)
        if not os.path.isfile(enc_path):
            raise FileNotFoundError(f"Encrypted media file not found: {enc_path}")
        enc_size = os.path.getsize(enc_path)
        if enc_size < 512:
            raise ValueError(
                f"Encrypted media file is too small ({enc_size} bytes). "
                "The download may have failed or been blocked."
            )

        drm_label = (label or "0").strip()
        keys_arg = f"--keys=label={drm_label}:key_id={key_id}:key={key}"
        # stream=0 matches the original module (works with v3.7.2 on dev); stream=audio is a fallback.
        stream_variants = [
            f"input={enc_path},stream=0,output={out_path}",
            f"input={enc_path},stream=audio,output={out_path},drm_label={drm_label}",
        ]

        env = get_clean_env()
        packager_dir = str(executable.parent)
        env["PATH"] = packager_dir + os.pathsep + env.get("PATH", "")
        last_detail = ""
        last_returncode = 1
        packager_args: list[str] = []

        for stream_descriptor in stream_variants:
            if os.path.isfile(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass

            packager_args = [
                stream_descriptor,
                "--enable_raw_key_decryption",
                keys_arg,
            ]
            run_kwargs = {
                "args": [str(executable), *packager_args],
                "env": env,
                "cwd": os.path.dirname(enc_path) or os.getcwd(),
                "capture_output": True,
                "text": True,
            }
            if sys.platform == "win32":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(**run_kwargs)
            last_returncode = result.returncode
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            last_detail = stderr or stdout or f"exit code {result.returncode}"

            if result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                LOGGER.debug(
                    "Shaka Packager OK (%s bytes decrypted)",
                    os.path.getsize(out_path),
                )
                return

            if result.returncode == 0:
                last_detail = (
                    f"Packager exited 0 but output missing or empty: {out_path} "
                    f"(encrypted input {enc_size} bytes)"
                )
            LOGGER.warning(
                "Shaka Packager attempt failed (exit %s): %s — %s",
                result.returncode,
                stream_descriptor,
                last_detail[:500],
            )

        if ModuleInterface._try_mp4decrypt(enc_path, out_path, key_id, key):
            return

        if last_returncode == 3221225477 or last_returncode == -1073741819:
            last_detail = (
                f"{last_detail}\n"
                "Shaka Packager crashed on this system (exit 3221225477). "
                "Place mp4decrypt.exe (Bento4) next to packager-win-x64.exe as a fallback — "
                "https://www.bento4.com/downloads/ → Windows binaries zip → mp4decrypt.exe. "
                "Or use a full Windows 10 VM instead of Tiny10."
            )
        elif not resolve_mp4decrypt():
            last_detail = (
                f"{last_detail}\n"
                "Optional: add mp4decrypt.exe from Bento4 next to the app folder "
                "(https://www.bento4.com/downloads/) when Shaka Packager fails."
            )
        raise subprocess.CalledProcessError(
            last_returncode,
            [str(executable), *packager_args],
            last_detail.strip(),
        )
