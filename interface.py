import base64
import logging
from pathlib import Path
import re
import shutil
import socket
import subprocess
import typing
import json
import dataclasses
import itertools
from datetime import datetime
from urllib.parse import urlparse
from uuid import UUID, uuid1, uuid4

import aria2p
import ffmpeg
import natsort
from pywidevine import PSSH, Cdm, Device
from tqdm import tqdm
from xml.etree import ElementTree


from utils.models import *
from utils.utils import create_temp_filename, download_file, silentremove

from .azapi import AmazonMusicMobileAPI, AmazonMusicMobileAPICredentials

LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class AudioTrack:
    asin: str
    codec: CodecEnum
    bitrate: int
    """ As kilobits per second (kbps) """
    sample_rate: int
    url: str
    official_quality_name: str
    quality: str
    quality_ranking: int
    pssh: PSSH
    bit_depth: Optional[int] = None

    def to_dict(self):
        return dataclasses.asdict(self)


# This is a Amazon Music module for OrpheusDL, require an active unlimited subscription

module_information = ModuleInformation(  # Only service_name and module_supported_modes are mandatory
    service_name="Amazon Music",
    module_supported_modes=ModuleModes.download | ModuleModes.lyrics,
    # | ModuleModes.covers,
    # | ModuleModes.credits,
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
        "max_track_quality_to_use": "",
    },
    global_storage_variables=[],
    session_settings={
        "email": "",
        "password": "",
        "country": "",
        "country_tld": "",
    },
    session_storage_variables=["credentials"],
    netlocation_constant="amazon",
    test_url="https://music.amazon.com/albums/B08TZPYLJN",
    login_behaviour=ManualEnum.manual,  # setting to ManualEnum.manual disables Orpheus automatically calling login() when needed
    url_decoding=ManualEnum.manual,  # setting to ManualEnum.manual disables Orpheus' automatic url decoding which works as follows:
)


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        self.settings = module_controller.module_settings
        self.options = module_controller.orpheus_options
        self.module_controller = module_controller
        self.print = module_controller.printer_controller.oprint

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

        # if not module_controller.orpheus_options.disable_subscription_check and (
        #     self.quality_parse[module_controller.orpheus_options.quality_tier]
        #     > self.mobile_session.get_user_tier()
        # ):
        #     print(
        #         "Example: quality set in the settings is not accessible by the current subscription"
        #     )

        self.cdm = Cdm.from_device(Device.load(self.settings["wvd_path"]))

        creds = module_controller.temporary_settings_controller.read("credentials")
        credentials = (
            AmazonMusicMobileAPICredentials.from_dict(creds) if creds else None
        )

        if credentials:
            self.mobile_session = AmazonMusicMobileAPI(credentials=credentials)
        else:
            self.login_onto_mobile(self.settings["email"], self.settings["password"])

        if self.mobile_session.credentials.access_token_expired:
            self.mobile_session.refresh_access_token()

        module_controller.temporary_settings_controller.set(
            "credentials", self.mobile_session.credentials.to_dict()
        )
        LOGGER.debug(self.mobile_session.credentials)

    def login_onto_mobile(
        self, email: str, password: str
    ):  # Called automatically by Orpheus when standard_login is flagged, otherwise optional
        mobile_session = AmazonMusicMobileAPI.login_via_mobile(
            email, password, self.settings["country_tld"], self.settings["country"]
        )
        if not mobile_session:
            raise Exception("Login failed")

        self.mobile_session = mobile_session
        LOGGER.debug(self.mobile_session._retrieve_capability())

        return self.mobile_session

    def custom_url_parse(self, link: str) -> MediaIdentification:
        url = urlparse(link)

        if not url.netloc.endswith(self.mobile_session.credentials.tld):
            raise ValueError(
                f"You must provide a URL that is within the same region as the account!"
            )

        queries = url.query.split("&")
        # print(url)
        # check if trying to download a track
        for query in queries:
            if query.startswith("trackAsin="):
                return MediaIdentification(
                    media_type=DownloadTypeEnum.track, media_id=query.split("=")[1]
                )

        components = url.path.split("/")

        # use the same logic as orpheusdl cuz lazy
        url_constants = {
            "albums": DownloadTypeEnum.album,
            # "playlists": DownloadTypeEnum.playlist,  # TODO
            "artists": DownloadTypeEnum.artist,
        }

        type_matches = [
            media_type
            for url_check, media_type in url_constants.items()
            if url_check in components
        ]

        return MediaIdentification(
            media_type=type_matches[-1],
            media_id=components[-1] if type_matches[-1] == "albums" else components[2],
        )

    def get_track_info(
        self,
        track_id: str,
        quality_tier: QualityEnum,
        codec_options: CodecOptions,
        data: dict = {},
    ) -> TrackInfo:  # Mandatory
        if (
            codec_options.spatial_codecs is False
            and not self.settings["force_non_spatial"]
        ):
            self.print(
                f"{module_information.service_name}: Warning: force_non_spatial is not set to True in settings.json"
            )
            self.print(f"Spatial codecs will be downloaded unless this is changed!")
        try:
            # quality_to_use = self.quality_parse[quality_tier]

            track_data = (
                data[track_id]
                if data and track_id in data
                else self.mobile_session.get_track_info(track_id)
            )
            album_id = str(track_data["album"]["asin"])
            album_data = (
                data[album_id]
                if data and album_id in data
                else self.mobile_session.get_album_info(album_id)
            )
            album_id = str(album_data["asin"])

            # NOTE: I *could* use catalog_track AND catalog album
            # but I prefer to limit the amount of API Requests per track
            search_data = (
                data[f"{album_id}_search"]
                if data and f"{album_id}_search" in data
                else self.mobile_session.search(
                    query='"{}", "{}"'.format(
                        album_data["artist"]["name"],
                        album_data["title"],
                        # track_data["title"],
                    ),
                    asins=(album_id, track_id, str(track_data["globalAsin"])),
                    search_types=("catalog_album",),
                )
                or {}
            )

            release_datetime = self._get_date_from_metadata(album_data)
            mapped_audio_tracks = (
                data[f"{track_id}_quality_mapping"]
                if data and f"{track_id}_quality_mapping" in data
                else None
            )
            if not mapped_audio_tracks:
                asin, mpd = self.mobile_session.get_track_manifest(track_id)
                if not (asin and mpd):
                    raise RuntimeError(
                        f"Failed to obtain track manifest for {track_id}"
                    )
                mapped_audio_tracks = self.mpd_to_quality_map(mpd, asin)

            track_to_use = self._get_usable_audio_track_of_mapped_quailty(
                mapped_audio_tracks,
                quality_tier,
                to_print=True,
            )

            contributors: list[str] = []
            # NOTE the main artist is inside contributors only if the album as one or more contributors
            if contributor_asins := tuple(track_data["artist"]["contributorAsins"]):
                contributors.extend(
                    str(item["name"])
                    for item in self.mobile_session.get_metadata(contributor_asins)[
                        "artistList"
                    ]
                )
            else:
                # Fallback, include the formatted artist name (might include contributors)
                contributors.append(album_data["artist"]["name"])
            
            # Attempt to seperate each contributor if they're concatenated
            for contributor in contributors.copy():
                contributor_sep = {
                    item
                    for item in re.split(r", | & ", contributor)
                    if item
                }
                if contributor_sep:
                    contributors.extend(
                        contributor_sep
                    )
                    contributors.remove(contributor)

            # Calculate the total disc avaliable by iterating each track and using the highest value
            disc_total = max(int(t["discNum"]) for t in album_data.get("tracks", [{}]))
            writers = list(track_data.get("songWriters", []))

            # Sometimes writers are just one item in a list, seperated with /
            # e.g https://music.amazon.co.jp/albums/B0CG7FWYTK
            if any(" / " in item for item in writers):
                for item in writers:
                    if " / " not in item:
                        continue
                    writers.extend(str(item).split(" / "))
                    writers.remove(item)

            composers = natsort.natsorted(set(writers))

            composers = "; ".join(composers)

            url = f"https://music.amazon.{self.mobile_session.credentials.tld}/albums/{album_id}?trackAsin={track_id}"

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
            genres = [
                # sanitize and format the genre name from search data
                # this genre tends to be more specific however,
                # it may not be always avaliable
                # e.g https://music.amazon.co.jp/albums/B09JNRPVHN?trackAsin=B09JNRQQ3P
                " ".join(
                    re.split("_| ", str(search_data.get("primaryGenre", "")))
                ).title()
            ]
            if album_data["productDetails"]["primaryGenreName"] not in genres:
                # this genre name tends to be broad
                genres.append(album_data["productDetails"]["primaryGenreName"])

            tags = Tags(
                album_artist=album_data["primaryArtistName"],
                composer=composers,
                copyright=album_data["productDetails"]["copyright"],
                isrc=track_data["isrc"],
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

            artwork_url = search_data.get("artOriginal", {}).get("artUrl")
            if not artwork_url:
                self.print(
                    "Failed to get original artwork, using lower resolution cover art.."
                )
                artwork_url = album_data.get("image", "")

            return TrackInfo(
                name=track_data["title"],
                album_id=album_id,
                album=track_data["album"]["title"],
                artists=contributors,
                tags=tags,
                codec=track_to_use.codec,
                cover_url=artwork_url,  # make sure to check module_controller.orpheus_options.default_cover_options
                release_year=int(release_datetime.strftime("%Y")),
                explicit=track_data["parentalControls"]["hasExplicitLanguage"],
                artist_id=track_data["artist"]["asin"],  # optional
                duration=track_data["duration"],
                # animated_cover_url="",  # optional
                # description="",  # optional
                bit_depth=track_to_use.bit_depth,  # optional
                sample_rate=track_to_use.sample_rate,  # optional
                bitrate=track_to_use.bitrate,  # optional
                download_extra_kwargs={
                    "audio_track": track_to_use,
                },  # optional only if download_type isn't DownloadEnum.TEMP_FILE_PATH, whatever you want
                # cover_extra_kwargs={
                #     "data": {track_id: ""}
                # },  # optional, whatever you want, but be very careful
                # credits_extra_kwargs={
                #     "data": {track_id: ""}
                # },  # optional, whatever you want, but be very careful
                # lyrics_extra_kwargs={
                #     "data": {track_id: ""}
                # },  # optional, whatever you want, but be very careful
                error="",  # only use if there is an error
            )
        except Exception as e:
            LOGGER.error(e, exc_info=True)
            raise e

    # def get_track_download(self, file_url: str, codec: CodecEnum, pssh: PSSH, **kwargs):
    def get_track_download(self, audio_track: AudioTrack, **kwargs):
        session_id = None
        try:
            os.makedirs("temp/", exist_ok=True)
            encrypted_track_location = f"{create_temp_filename()}.mp4"
            self.download(
                audio_track.url,
                encrypted_track_location,
                use_aria2c=self.settings["prefer_aria2c"],
            )

            # decrypt the file (attempt to request a license)
            session_id = self.cdm.open()
            license_challenge = base64.b64encode(
                self.cdm.get_license_challenge(
                    session_id, audio_track.pssh, privacy_mode=False
                )
            ).decode("utf-8")

            license_response = self.mobile_session.get_license_response(
                asin=audio_track.asin, challenge=license_challenge
            )
            if not license_response:
                license_response = input(
                    "License retrieval failed, enter response here: "
                )
            self.cdm.parse_license(session_id, license_response)

            decrypted_track_location = f"{create_temp_filename()}.mp4"  # {codec_data[audio_track.codec].container.name}

            self.cdm.decrypt(
                session_id,
                encrypted_track_location,
                decrypted_track_location,
                exists_ok=True,
            )
            LOGGER.debug("Ok wth decryption")
            self.cdm.close(session_id)
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
            ffmpeg.input(decrypted_track_location).output(
                final_decrypted_track_location,
                loglevel="warning",
                audio_bitrate=f"{audio_track.bitrate}k",
            ).run()
            silentremove(decrypted_track_location)

        except Exception as e:
            LOGGER.error(e, exc_info=True)
            if session_id:
                self.cdm.close(session_id)
            return

        return TrackDownloadInfo(
            download_type=DownloadEnum.TEMP_FILE_PATH,
            temp_file_path=final_decrypted_track_location,
        )

    def get_album_info(self, album_id: str, data: dict = {}) -> Optional[AlbumInfo]:
        LOGGER.debug("Getting album info")

        album_data = dict(
            data[album_id]
            if album_id in data
            else self.mobile_session.get_album_info(album_id)
        )
        # Force use the ASIN the API returns with
        album_id = album_data["asin"]
        search_data = dict(
            data[f"{album_id}_search"]
            if data and f"{album_id}_search" in data
            else self.mobile_session.search(
                query=f'"{album_data["artist"]["name"]}" - "{album_data["title"]}"',
                asins=(album_id, album_data["requestedAsin"]),
                limit=100,
            )
        )

        cover_url = search_data.get("artOriginal", {}).get(
            "artUrl", album_data.get("image")
        )

        # Scan through the first 10 tracks (to limit delay if force_album_format)
        # I think httpx.Client.send is CPU bounded..
        mapped_tracks = {
            f"{asin}_quality_mapping": self.mpd_to_quality_map(mpd, asin)
            for asin, mpd in self.mobile_session.get_tracks_manifest(
                track["asin"] for track in album_data.get("tracks", [])[:10]
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
        )

        return AlbumInfo(
            name=album_data.get(
                "title", "Unknown name"
            ),  # "".join(str(album_data.get("title", "Unknown")).rsplit("[Explicit]")),
            artist=album_data.get("primaryArtistName", ""),
            tracks=[track["asin"] for track in album_data.get("tracks", [])],
            release_year=int(self._get_date_from_metadata(album_data).strftime("%Y")),
            # Uncomment if you wish to add [E] (Amazon already returns [Explicit] inside the name)
            # explicit=any(track["parentalControls"]["hasExplicitLanguage"] for track in album_data.get("tracks", [])),
            duration=album_data.get("duration"),
            artist_id=album_data.get("artist", {}).get("asin"),  # optional
            # booklet_url="",  # optional
            cover_url=cover_url,  # optional
            cover_type=ImageFileTypeEnum.jpg,  # optional
            all_track_cover_jpg_url=cover_url,  # technically optional, but HIGHLY recommended
            animated_cover_url="",  # optional
            description="",  # optional
            quality=f"{best_audio_track.official_quality_name}] [{codec_data[best_audio_track.codec].pretty_name}",
            track_extra_kwargs={
                "data": {track["asin"]: track for track in album_data.get("tracks", [])}
                | {album_id: album_data}
                | {f"{album_id}_search": search_data}
                | mapped_tracks
            },  # optional, whatever you want
        )

    def get_playlist_info(
        self, playlist_id: str, data={}
    ) -> (
        PlaylistInfo
    ):  # Mandatory if either ModuleModes.download or ModuleModes.playlist
        raise NotImplementedError
        playlist_data = (
            data[playlist_id]
            if playlist_id in data
            else self.mobile_session.get_playlist(playlist_id)
        )

        return PlaylistInfo(
            name="",
            creator="",
            tracks=[],
            release_year="",
            explicit=False,
            creator_id="",  # optional
            cover_url="",  # optional
            cover_type=ImageFileTypeEnum.jpg,  # optional
            animated_cover_url="",  # optional
            description="",  # optional
            track_extra_kwargs={"data": ""},  # optional, whatever you want
        )

    def get_artist_info(
        self, artist_id: str, get_credited_albums: bool
    ) -> ArtistInfo:  # Mandatory if ModuleModes.download
        # get_credited_albums means stuff like remix compilations the artist was part of
        try:
            artist_data = self.mobile_session.get_metadata(artist_id)["artistList"][0]
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
        for album in self.mobile_session.search(
            query=f'"{artist_name}"',
            search_types=("catalog_album",),
            limit=1000,
        ):
            if artist_name not in album.get("artistName", ""):
                continue
            album_metadata = self.mobile_session.get_album_info(album["asin"])
            # Filter out foreign albums with the same artist name (but not ID)
            if (
                not album_metadata["artist"]["contributorAsins"]
                and album_metadata["artist"]["asin"] != artist_id
            ):
                continue

            # Assume that if they aren't equal, they are credited albums
            # artist_id != album.get("artistAsin")
            if (artist_name != album.get("artistName", "")) and not get_credited_albums:
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
        self, track_id: str, data={}
    ):  # Mandatory if ModuleModes.credits
        track_data = (
            data[track_id]
            if track_id in data
            else self.mobile_session.get_track(track_id)
        )
        credits = track_data["credits"]
        credits_dict = {}
        return [CreditsInfo(k, v) for k, v in credits_dict.items()]

    def get_track_cover(
        self, track_id: str, cover_options: CoverOptions, data={}
    ) -> CoverInfo:  # Mandatory if ModuleModes.covers
        track_data = dict(
            (
                data[track_id]
                if track_id in data
                else self.mobile_session.get_track_info(track_id)
            )
        )
        print(self.mobile_session.get_track_info(track_id))
        cover_url: str | None = str(track_data.get("album", {}).get("image") or None)
        return CoverInfo(
            url=cover_url,
            file_type=ImageFileTypeEnum.jpg
            if cover_url and cover_url.endswith("jpg")
            else None,
        )

    def get_track_lyrics(
        self, track_id: str, data={}
    ) -> LyricsInfo:  # Mandatory if ModuleModes.lyrics
        track_lyrics_resp = self.mobile_session.get_track_lyrics(track_id)

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
        if (
            query_type is DownloadTypeEnum.artist
            or query_type is DownloadTypeEnum.playlist
        ):
            # super lazy
            raise TypeError(f"{query_type} is not supported yet!")

        results = {}
        search_type = {item.name: f"catalog_{item.name}" for item in DownloadTypeEnum}[
            query_type.name
        ]
        # if track_info and track_info.tags.isrc:
        #     results = list(self.mobile_session.get_documents_from_search_results(
        #         self.mobile_session.search(
        #             search_type, track_info.tags.isrc, limit
        #         )
        #     ))
        if not results:
            results = list(
                self.mobile_session.search(
                    query=query, search_types=(search_type,), limit=limit
                )
            )

        return [
            SearchResult(
                result_id=i["asin"],
                name=i["title"],  # optional only if a lyrics/covers only module
                artists=[
                    i["artistName"]
                ],  # optional only if a lyrics/covers only module or an artist search
                year=datetime.fromtimestamp(
                    round(float(str(i.get("originalReleaseDate"))))
                ).strftime(
                    "%Y"
                ),  # optional
                explicit=i["parentalControls"]["hasExplicitLanguage"],  # optional
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
                ),  # optional, used to convey more info when using orpheus.py search (not luckysearch, for obvious reasons)
                extra_kwargs={
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

    @staticmethod
    def _get_date_from_metadata(album_data: dict[str, typing.Any]):
        return datetime.fromtimestamp(
            int(
                str(
                    album_data.get("originalReleaseDate")
                    or album_data.get("merchantReleaseDate")
                )
            )
            / 1000
        )

    def download(self, url: str, location: str, use_aria2c: typing.Optional[bool]):
        # Attempt to use download with aria2c (faster)
        # Otherwise use the OrpheusDL default method
        aria2c_bin = shutil.which("aria2c")
        if aria2c_bin and use_aria2c:
            # with YoutubeDL(
            #     {
            #         "quiet": True,
            #         "no_warnings": True,
            #         "outtmpl": location,  # must be a path including the filename
            #         "allow_unplayable_formats": True,
            #         "fixup": "never",
            #         "overwrites": True,
            #         "external_downloader": "aria2c",
            #         # "logger": LOGGER
            #     }
            # ) as ydl:
            #     ydl.download(url)

            ModuleInterface.download_with_aria2c(url, location)
        else:
            download_file(
                url,
                location,
                enable_progress_bar=True,
            )

    @staticmethod
    def milliseconds_to_lrc_time(milliseconds: int):
        # Convert milliseconds to the proper LRC time format [mm:ss.xx]
        return f"{milliseconds // 60000:02}:{(milliseconds // 1000) % 60:02}.{milliseconds % 1000:03}"

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

            # Funny progress bar to maintain consistency
            with tqdm(
                total=download.total_length,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
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
        track_to_use = None
        # I tried, ok?
        # Attempt to find and choose preferred quality (has to be the same returned in the MPD)
        if max_track_quality_to_use := self.settings["max_track_quality_to_use"]:
            max_track_quality_to_use = str(max_track_quality_to_use).upper()
            for (
                quality_enum,
                quality_name,
                tracks,
            ) in self._iter_over_tracks_to_quality_map(mapped_audio_tracks):
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

        # If max_track_quality_to_use is not set or failed, then use the global quality to use
        if not track_to_use:
            for (
                quality_enum,
                quality_name,
                tracks,
            ) in self._iter_over_tracks_to_quality_map(mapped_audio_tracks):
                # self.print(f"{quality_enum=}, {quality_name=}, {tracks=}")
                if not tracks:
                    continue

                if len(tracks) > 1 and to_print:
                    LOGGER.warning(
                        f"There are more than one tracks avaliable for {quality_name}. "
                        f"Avaliable qualities: "
                        ", ".join(
                            f"{item.quality} with ranking {item.quality_ranking}"
                            for item in tracks
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

    def mpd_to_quality_map(self, mpd: ElementTree.Element, track_asin: str):
        """
        A helper function to retrieve a mapping of a QualityEnum
        to a dictionary of a Quality and a list of AudioTracks via a MPD.

        """
        # LOGGER.debug(json.dumps(mpd, indent=3))
        avaliable_tracks = self._mpd_to_audio_track(mpd, track_asin)

        # Sorting by bitrate helps retrieve the highest resolution avaliable
        LOGGER.debug(avaliable_tracks)

        preferred_codecs = [CodecEnum.OPUS, CodecEnum.FLAC]

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
            avaliable_tracks, preferred_codecs
        )
        LOGGER.debug(mapped_audio_tracks)
        return mapped_audio_tracks

    def _mpd_to_audio_track(self, manifest: ElementTree.Element, track_asin: str):
        """
        Iterate over the manifest to grab the tracks and append them to avaliable_tracks as a AudioTrack object


        (which is a xml file that follows the urn:mpeg:dash:profile:isoff-on-demand:2011 profile)
        """

        # manifest = dict(mpd["MPD"]["Period"])
        avaliable_tracks: list[AudioTrack] = []

        for period in manifest.findall("Period"):
            for adaptation_set in period.findall("AdaptationSet"):
                content_type = adaptation_set.get("contentType")
                if content_type != "audio":
                    raise ValueError("Only supports audio MPDs!")

                pssh = None

                for content_property in adaptation_set.findall("ContentProtection"):
                    if (
                        content_property.get("schemeIdUri")
                        != "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
                    ):
                        continue

                    pssh_elem = content_property.find("{urn:mpeg:cenc:2013}pssh")

                    if not (pssh_elem is not None and pssh_elem.text):
                        continue
                    pssh = PSSH(pssh_elem.text, strict=True)
                    break

                else:
                    raise ValueError("Failed to find PSSH.")

                supplemental_properties = adaptation_set.findall("SupplementalProperty")
                for prop in supplemental_properties:
                    if prop.get("schemeIdUri") != "amz-music:trackType":
                        continue
                    official_quality_name = prop.get("value", "Unknown")
                    LOGGER.debug(f"Official name for track: {official_quality_name}")
                    break
                else:
                    # impossible unless amazon changes their backend (never)
                    raise RuntimeError(
                        [item.attrib for item in supplemental_properties]
                    )

                for representation in adaptation_set.findall("Representation"):
                    media_url_elem = representation.find("BaseURL")
                    if not (media_url_elem is not None and media_url_elem.text):
                        raise IndexError(f"Failed to find Media URL for {track_asin}")
                    media_url = media_url_elem.text

                    muq = iter(re.split("=|&", urlparse(media_url).query))
                    media_url_query = dict(zip(muq, muq))
                    quality = str(media_url_query.get("ql"))

                    if quality.startswith(
                        "UHD"
                    ) and not official_quality_name.startswith("UHD"):
                        # amz-music:trackType only returns the following:
                        # LD, SD, HD, and 3D
                        official_quality_name = "UHD"

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

                    codec = CodecEnum[codec]

                    bit_depth = None
                    if codec is CodecEnum.FLAC:
                        sp = representation.find("SupplementalProperty")
                        if sp is not None:
                            bit_depth = int(sp.get("value", 0))

                    avaliable_tracks.append(
                        AudioTrack(
                            asin=track_asin,
                            codec=codec,
                            bit_depth=bit_depth,
                            bitrate=int(
                                int(representation.get("bandwidth") or 0) / 1000
                            ),  # im not exactly sure why i have to divide it by 1000 but meh
                            sample_rate=int(
                                representation.get("audioSamplingRate") or 0
                            ),
                            url=media_url,
                            official_quality_name=official_quality_name,
                            quality_ranking=int(
                                representation.get("qualityRanking", 0)
                            ),
                            quality=quality,
                            pssh=pssh,
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
        mapping: dict[QualityEnum, dict[str, list[AudioTrack]]]
    ):
        for quality_enum, quality_items in mapping.items():
            for quality_name, tracks in quality_items.items():
                yield quality_enum, quality_name, tracks
        return

    def tracks_to_quality_map(
        self,
        tracks: typing.Iterable[AudioTrack],
        preferred_codecs: typing.Iterable[CodecEnum],
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
                if "ATMOS" in track.quality:
                    return track.bitrate
                if "LD" in track.quality:
                    return track.bitrate
                if quality_enum.value <= QualityEnum.LOSSLESS.value:
                    return track.quality_ranking

            def key_for_filtering_audiotracks(track: AudioTrack):
                for quality in qualities:
                    if not (
                        track.quality.startswith(quality)
                        and track.codec in preferred_codecs
                    ):
                        continue
                    return True
                return False

            def key_for_grouping_audiotracks(track: AudioTrack):
                return track.quality

            # AudioTracks are sorted best quality to worse

            grouped_tracks = {
                key: natsort.natsorted(
                    group, key=key_for_sorting_avaliable_tracks, reverse=True
                )
                for key, group in itertools.groupby(
                    [item for item in tracks if key_for_filtering_audiotracks(item)],
                    key=key_for_grouping_audiotracks,
                )
            }
            # import pprint
            # pprint.pprint(grouped_tracks)

            quality_to_track_mapping.update({quality_enum: grouped_tracks})

        return quality_to_track_mapping
