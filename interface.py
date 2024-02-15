import base64
from collections import OrderedDict
import functools
import logging
import os
import itertools
import random
import re
import shutil
import socket
import subprocess
import sys
import typing
import dataclasses
import itertools
import httpx
from datetime import datetime, timedelta
import concurrent.futures
from urllib.parse import urlparse, parse_qs

import aria2p
import ffmpeg
import natsort
from pywidevine import PSSH, Cdm, Device
from pywidevine.utils import get_binary_path
from pywidevine.license_protocol_pb2 import WidevinePsshData
from Crypto.Cipher import AES

from tqdm import tqdm
from xml.etree import ElementTree

from utils.models import *
from utils.utils import (
    create_temp_filename,
    download_file,
    silentremove,
    sanitise_name,
)

from modules.amazonmusic.models import AmazonContinent
from .azapi import AmazonMusicMobileAPI, AmazonMusicMobileAPICredentials, AmazonMusicTier, AmazonRegion

LOGGER = logging.getLogger(__name__)

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
        "quality_format": "{official_quality_name} {codec_pretty_name} {bit_depth}bit-{sample_rate}kHz",
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
        if not self.settings["force_non_spatial"]:
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

        self.cdm = Cdm.from_device(Device.load(self.settings["wvd_path"]))

        if self.settings["force_login"] or not self.load_cached_mobile_session(check_only=True):
            mobile_session = self.login_onto_mobile(self.settings["email"], self.settings["password"])

    def login_onto_mobile(
        self, email: str, password: str
    ):  # Called automatically by Orpheus when standard_login is flagged, otherwise optional
        if not self.settings["country"]:
            raise ValueError(
                "Please fill in country before trying to login!"
            )
        self.print(
            (
                f"{module_information.service_name}: Logging on using "
                f"{AmazonRegion.get_region_by_country(self.settings['country']).pretty_name} as the region."
            )
        )
        mobile_session = AmazonMusicMobileAPI.login_via_mobile(
            email, password, self.settings["country"]
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
                    f"{module_information.service_name}: Using a {new_region.pretty_name!r} account "
                    f"for the region {selected_region.pretty_name!r} as you are not logged into said region."
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
            f"{module_information.service_name}: The selected {selected_region.pretty_name!r} account "
            f"currently has a {session.credentials.tier.name.title()!r} subscription "
            f"with the usage of entitlements "
            f"{'enabled.' if entitlement_usage else 'disabled.'}"
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
                            album_data["productDetails"]["primaryGenreName"]
                        )
                    )
                    if item
                ),
                key=len
            )
            tags = Tags(
                album_artist=primary_artist_name,
                composer=composers,
                copyright=album_data["productDetails"]["copyright"],
                isrc=track_data.get("isrc"),
                # upc="",
                disc_number=int(track_data["discNum"] or 1),
                total_discs=disc_total,
                track_number=int(track_data["trackNum"] or 1),  # None/0/1 if no discs
                total_tracks=int(album_data["trackCount"] or 1),  # None/0/1 if no discs
                # replay_gain=0.0,
                # replay_peak=0.0,
                genres=genres,
                label=album_data["productDetails"]["label"],
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
            if not os.path.exists(decrypted_track_location):
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

        album_data = dict(
            data[album_id]
            if album_id in data
            else mobile_session.get_album_info(
                album_id,
                use_alternative_naming=False,
                region_to_use=media_region
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

        cover_url, search_data = self.get_hi_res_cover(
            asins=valid_asins,
            query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
            search_data=data.get(f"{album_id}_search"),
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

        best_audio_track = max(
            [
                self._get_usable_audio_track_of_mapped_quailty(
                    mapped_audio_tracks, self.options.quality_tier
                )
                for mapped_audio_tracks in mapped_tracks.values()
            ],
            key=lambda i: i.bitrate,
            default=None
        )
        if not best_audio_track:
            raise TypeError("No available tracks.")
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
            track["parentalControls"]["hasExplicitLanguage"]
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
            quality=str(self.settings.get("quality_format", "")).format(**quality_tags),
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

        p_data = data[playlist_id] if playlist_id in data else {}

        if not p_data:
            if len(playlist_id) == 10:
                # An ASIN
                p_data = mobile_session.get_catalog_playlist(playlist_id, region_to_use=media_region)[
                    "playlist"
                ]
            else:
                # A user playlist (either from the shared link, or the address bar)
                p_data = mobile_session.get_user_playlist(playlist_id)[
                    "playlists"
                ][0]

        p_data = dict(p_data)
        return PlaylistInfo(
            name=p_data["metadata"]["title"],
            creator=p_data.get("metadata", {}).get("curatedBy", ""),
            tracks=[track["metadata"]["asin"] for track in p_data.get("tracks", [])],
            release_year=0,
            duration=p_data["metadata"]["durationSeconds"],
            explicit=p_data.get("metadata", {})
            .get("parentalControls", {})
            .get("hasExplicitLanguage", False),
            creator_id=p_data.get("metadata", {}).get("profileId"),  # optional
            cover_url=p_data["metadata"]["fourSquareArt"]["url"],  # optional
            cover_type=ImageFileTypeEnum.jpg,  # optional
            description=p_data.get("metadata", {}).get("description"),  # optional
            track_extra_kwargs={
                "media_region": media_region,
                "data": {
                    f'{track["metadata"]["asin"]}_playlist': track["metadata"]
                    for track in p_data.get("tracks", [])
                }
            },  # optional, whatever you want
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
                    limit=1000,
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

    def search(
        self,
        query_type: DownloadTypeEnum,
        query: str,
        track_info: typing.Optional[TrackInfo] = None,
        limit: int = 10,
    ):  # Mandatory
        if query_type is DownloadTypeEnum.playlist:
            # super lazy
            raise TypeError(f"{query_type} is not supported yet!")
        mobile_session, media_region = self.select_session(
            random.choice(
                AmazonRegion.get_available_regions_by_continent(
                    self.settings["prefer_account_continent"]
                )
            )
        )
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

        return [
            SearchResult(
                result_id=i["asin"],
                name=i.get("title")
                or i.get("name"),  # optional only if a lyrics/covers only module
                artists=[i["artistName"]]
                if i.get("artistName")
                else None,  # optional only if a lyrics/covers only module or an artist search
                year=datetime.fromtimestamp(
                    round(float(str(i.get("originalReleaseDate"))))
                ).strftime("%Y")
                if query_type in (DownloadTypeEnum.track, DownloadTypeEnum.album)
                else None,  # optional
                explicit=i["parentalControls"]["hasExplicitLanguage"]
                if i.get("parentalControls")
                else None,  # optional
                additional=natsort.natsorted(
                    map(
                        lambda item: {
                            "hdAvailable": "HD",
                            "uhdAvailable": "UHD",
                            "atmosAvailable": "Dolby Atmos",
                            "ra360Available": "360 Reality Audio",
                            "immersive": "Immersive Audio",
                        }[item],
                        i.get("contentEncoding", []),
                    ),
                    key=len,
                )
                + [
                    i.get("asin")
                ],  # optional, used to convey more info when using orpheus.py search (not luckysearch, for obvious reasons)
                extra_kwargs={
                    "media_region": media_region,
                    "data": {f"{i['asin']}_search": i}
                }  # optional, whatever you want. NOTE: BE CAREFUL! this can be given to:
                # get_track_info, get_album_info, get_artist_info with normal search results, and
                # get_track_credits, get_track_cover, get_track_lyrics in the case of other modules using this module just for those.
                # therefore, it's recommended to choose something generic like 'data' rather than specifics like 'cover_info'
                # or, you could use both, keeping a data field just in case track data is given, while keeping the specifics, but that's overcomplicated
            )
            for i in results
        ]

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
        # Attempt to use download with aria2c (faster but error prone)
        # Otherwise use the OrpheusDL default method
        if shutil.which("aria2c") and use_aria2c:
            self.download_with_aria2c(url, location)
        else:
            download_file(
                url,
                location,
                enable_progress_bar=True,
            )

    @classmethod
    def download_with_aria2c(cls, url: str, output_path: str):
        # I am not proud with this code, but it works so its fine for now
        aria2c_bin = shutil.which("aria2c")
        if not aria2c_bin:
            return False
        secret = os.urandom(16).hex()
        open_port = cls.find_available_port()

        rpc_proc = None

        try:
            rpc_proc = cls._open_aria2c_rpc(aria2c_bin, secret, open_port)
            session = aria2p.API(
                aria2p.Client(host="http://localhost", port=open_port, secret=secret)
            )
            download = session.add_uris([url], {"out": output_path})

            # Wait for the download to start
            while not download.is_active or not download.total_length:
                download.update()
                if download.error_message:
                    raise RuntimeError(download.error_code, download.error_message)

            # Funny progress bar to maintain consistency
            with tqdm(
                total=download.total_length,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                disable=False,
            ) as pbar:
                while download.is_active:
                    download.update()
                    pbar.update(download.completed_length - pbar.n)
                    pbar.refresh()
        finally:
            if rpc_proc:
                rpc_proc.kill()

    @classmethod
    def _open_aria2c_rpc(cls, aria2c_bin: str, secret: str, open_port: str | int):
        rpc_proc = subprocess.Popen(
            [
                aria2c_bin,
                "--enable-rpc",
                "--rpc-listen-all=true",
                "--rpc-allow-origin-all",
                f"--rpc-listen-port={open_port}",
                "--rpc-secret",
                secret,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        is_listening = False
        while not is_listening:
            if not rpc_proc.stdout:
                continue

            line = rpc_proc.stdout.readline()
            if not line or not line.split():
                continue
            # Find the logged message
            if match := re.search(
                r"\d{2}/\d{2} \d{2}:\d{2}:\d{2}\s+\[(.*?)\]\s+(.*)", line
            ):
                if "RPC: listening" not in match.group(2):
                    LOGGER.error("aria2c RPC: %s", match.group(2))
                    # Be sure the RPC dies before raising
                    # To prevent stray aria2c RPCs running
                    rpc_proc.kill()
                    raise ValueError(match.group(2))
            if f"RPC: listening on TCP port {open_port}" in line:
                is_listening = True

        # nobreak
        else:
            return rpc_proc

    @staticmethod
    def find_available_port():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        sock.bind(("localhost", 0))
        port = int(sock.getsockname()[1])  # Get the chosen port

        sock.close()

        return port

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

                if len(tracks) > 1 and to_print:
                    self.print(
                        (
                            f"{module_information.service_name}: "
                            f"There are more than one tracks avaliable for {quality_name}. "
                            f"\nAvaliable qualities: "
                        ) + 
                        (
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
        
        if not has_entitlements and subscription_tier is AmazonMusicTier.FREE:
            return {}

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
    def call_shaka_packager(encrypted_file: str, destination_file: str, key_id: str, key: str, label: str):
        platform = {"win32": "win", "darwin": "osx"}.get(sys.platform, sys.platform)
        executable = get_binary_path(f"packager-{platform}", f"packager-{platform}-x64", "shaka-packager", "packager")
        if not executable:
            raise EnvironmentError("Shaka Packager executable not found but is required")

        args = [
            f"input={encrypted_file},stream=0,output={destination_file}",
            "--enable_raw_key_decryption",
            f"--keys=label={label}:key_id={key_id}:key={key}",
            "--quiet"
        ]
        subprocess.check_call([executable, *args])
