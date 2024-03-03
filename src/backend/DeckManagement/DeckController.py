"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Import Python modules
from copy import copy
import os
import random
from threading import Timer
import threading
import time
from PIL import Image, ImageOps, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper
from StreamDeck.Devices import StreamDeck
import usb.core
import usb.util
from loguru import logger as log
import asyncio
import matplotlib.font_manager
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.Subclasses.key_video_cache import VideoFrameCache

# Import own modules
from src.backend.DeckManagement.HelperMethods import *
from src.backend.DeckManagement.ImageHelpers import *
from src.backend.PageManagement.Page import Page
from src.backend.DeckManagement.Subclasses.ScreenSaver import ScreenSaver

# Import signals
from src.backend.PluginManager import Signals

# Import typing
from typing import TYPE_CHECKING

from src.windows.mainWindow.elements.KeyGrid import KeyButton, KeyGrid
if TYPE_CHECKING:
    from src.windows.mainWindow.elements.DeckStackChild import DeckStackChild
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.backend.PluginManager.ActionBase import ActionBase

# Import globals
import globals as gl


class DeckController:
    def __init__(self, deck_manager: "DeckManager", deck: StreamDeck.StreamDeck):
        self.deck_manager: DeckManager = deck_manager
        self.deck: StreamDeck = deck
        # Check if deck is already open
        if not deck.is_open:
            # Open deck
            deck.open()
        
        # Clear the deck
        deck.reset()

        self.screen_saver = ScreenSaver(deck_controller=self)

        # Tasks
        self.media_player_tasks: dict = {}

        self.ui_grid_buttons_changes_while_hidden: dict = {}

        self.active_page: Page = None

        self.brightness:int = 75
        self.set_brightness(self.brightness)

        self.keys: list[ControllerKey] = []
        self.init_keys()

        self.background = Background(self)

        self.deck.set_key_callback(self.key_change_callback)

        self.load_default_page()

        # Start media player thread
        self.media_player_thread = threading.Thread(target=self.play_media)
        self.media_player_thread.start()

        self.TICK_DELAY = 1
        self.tick_timer = Timer(self.TICK_DELAY, self.tick_actions)
        self.tick_timer.start()

    def init_keys(self):
        self.keys: list[ControllerKey] = []
        for i in range(self.deck.key_count()):
            self.keys.append(ControllerKey(self, i))

    def add_media_player_task(self, method: callable, key: int, image):
        _id = random.randint(0, 10000)
        if _id in self.media_player_tasks:
            self.add_media_player_task(method, key, image)

        self.media_player_tasks[_id] = {
            "method": method,
            "key": key,
            "image": image
        }

    def update_key(self, index: int):
        image = self.keys[index].get_current_deck_image()
        native_image = PILHelper.to_native_format(self.deck, image.convert("RGB"))

        self.add_media_player_task(self.deck.set_key_image, index, native_image)
        
        self.keys[index].set_ui_key_image(image)

    def update_all_keys(self):
        if self.background.video is not None:
            log.debug("Skipping update_all_keys because there is a background video")
            return
        for i in range(self.deck.key_count()):
            self.update_key(i)
    def play_media(self):
        while True:
            start = time.time()
            if self.background.video is not None:
                # There is a background video
                self.background.update_tiles()

            for key in self.keys:
                if self.background.video is None and key.key_video is None:
                    continue
                key.update()

            # Perform media player tasks
            self.perform_media_player_tasks()

            # Wait for approximately 1/30th of a second before the next call
            end = time.time()
            # print(f"possible FPS: {1 / (end - start)}")
            wait = max(0, 1/60 - (end - start))
            time.sleep(wait)

    def perform_media_player_tasks(self):
        tasks = copy(self.media_player_tasks)
        for task in tasks:
            key = tasks[task]["key"]
            image = tasks[task]["image"]

            with self.deck:
                # print(f"updating key {key}")
                self.deck.set_key_image(key, image)

            # Remove the task if still in the list - might be removed by clear_media_player_tasks()
            if task in self.media_player_tasks:
                del self.media_player_tasks[task]

    def key_change_callback(self, deck, key, state):
        if state:
            # Only on key down this allows plugins to control screen saver without directly deactivating it
            self.screen_saver.on_key_change()
        
        if self.screen_saver.showing:
            return

        self.keys[key].on_key_change(state)

    ### Helper methods
    def generate_alpha_key(self) -> None:
        return Image.new("RGBA", self.get_key_image_size(), (0, 0, 0, 0))
    
    def get_key_image_size(self) -> tuple[int]:
        return self.deck.key_image_format()["size"]
    
    # ------------ #
    # Page Loading #
    # ------------ #

    def load_default_page(self):
        default_page_path = gl.page_manager.get_default_page_for_deck(self.deck.get_serial_number())
        if default_page_path is None:
            # Use the first page
            pages = gl.page_manager.get_pages()
            if len(pages) == 0:
                return
            default_page_path = gl.page_manager.get_pages()[0]

        if default_page_path is None:
            return
        
        page = gl.page_manager.create_page(default_page_path, self)
        self.load_page(page)

    def load_background(self, page: Page, update: bool = True):
        log.info(f"Loading background in thread: {threading.get_ident()}")
        deck_settings = self.get_deck_settings()
        def set_from_deck_settings(self: "DeckController"):
            if deck_settings.get("background", {}).get("enable", False):
                self.background.set_from_path(deck_settings.get("background", {}).get("path"), update=update)
            else:
                self.background.set_from_path(None, update=update)

        def set_from_page(self: "DeckController"):
            if not page.dict.get("background", {}).get("show", True):
                self.background.set_from_path(None, update=update)
            else:
                self.background.set_from_path(page.dict.get("background", {}).get("path"), update=update)

        if page.dict.get("background", {}).get("overwrite", False) is False and "background" in deck_settings:
            set_from_deck_settings(self)
        else:
            set_from_page(self)

    def load_brightness(self, page: Page):
        deck_settings = self.get_deck_settings()
        def set_from_deck_settings(self: "DeckController"):
            self.deck.set_brightness(deck_settings.get("brightness", {}).get("value", 75))

        def set_from_page(self: "DeckController"):
            self.deck.set_brightness(page.dict.get("brightness", 75))

        if "brightness" in deck_settings:
            set_from_deck_settings(self)
        else:
            set_from_page(self)

    def load_screensaver(self, page: Page):
        deck_settings = self.get_deck_settings()
        def set_from_deck_settings(self: "DeckController"):
            path = deck_settings.get("screensaver", {}).get("path")
            enable = deck_settings.get("screensaver", {}).get("enable", False)
            loop = deck_settings.get("screensaver", {}).get("loop", False)
            fps = deck_settings.get("screensaver", {}).get("fps", 30)
            time = deck_settings.get("screensaver", {}).get("time-delay", 5)

            self.screen_saver.set_media_path(path)
            self.screen_saver.set_enable(enable)
            self.screen_saver.set_time(time)
            # self.screen_saver.set_loop(loop) # TODO: implement
            # self.screen_saver.set_fps(fps) # TODO: implement

        def set_from_page(self: "DeckController"):
            path = page.dict.get("screensaver", {}).get("path")
            enable = page.dict.get("screensaver", {}).get("enable", False)
            loop = page.dict.get("screensaver", {}).get("loop", False)
            fps = page.dict.get("screensaver", {}).get("fps", 30)
            time = page.dict.get("screensaver", {}).get("time-delay", 5)

            self.screen_saver.set_media_path(path)
            self.screen_saver.set_enable(enable)
            self.screen_saver.set_time(time)
            # self.screen_saver.set_loop(loop) # TODO: implement
            # self.screen_saver.set_fps(fps) # TODO: implement

        if self.active_page.dict.get("screensaver", {}).get("overwrite", False) is False and "screensaver" in deck_settings:
            set_from_deck_settings(self)
        else:
            set_from_page(self)

    def load_all_keys(self, page: Page, update: bool = True):
        for key in self.keys:
            self.load_key(key.key, page, update)

    def load_key(self, key: int, page: Page, update: bool = True, load_labels: bool = True, load_media: bool = True):
        coords = self.index_to_coords(key)
        key_dict = page.dict.get("keys", {}).get(f"{coords[0]}x{coords[1]}", {})
        self.keys[key].load_from_page_dict(key_dict, update, load_labels, load_media)

    def load_page(self, page: Page):
        self.active_page = page

        if page is None:
            # Clear deck
            self.deck.reset()
            return

        log.info(f"Loading page {page.get_name()} on deck {self.deck.get_serial_number()}")

        self.load_brightness(page)
        self.load_screensaver(page)
        self.load_background(page, update=False)
        self.load_all_keys(page, update=False)

        # Clear unfinished tasks from old page
        self.clear_media_player_tasks()
        # Load new page onto deck
        self.update_all_keys()

    def set_brightness(self, value):
        self.deck.set_brightness(value)
        self.brightness = value

    def tick_actions(self) -> None:
        for key in self.keys:
            key.own_actions_tick()
        
        # Restart timer
        self.tick_timer = Timer(self.TICK_DELAY, self.tick_actions)
        self.tick_timer.start()


    # -------------- #
    # Helper methods #
    # -------------- #
        
    def index_to_coords(self, index):
        rows, cols = self.deck.key_layout()    
        y = index // cols
        x = index % cols
        return x, y
    
    def coords_to_index(self, coords):
        x, y = map(int, coords)
        rows, cols = self.deck.key_layout()
        return y * cols + x
    
    def get_deck_settings(self):
        return gl.settings_manager.get_deck_settings(self.deck.get_serial_number())
    
    def get_own_key_grid(self) -> KeyGrid:
        if not recursive_hasattr(gl, "app.main_win.leftArea.deck_stack"): return
        serial_number = self.deck.get_serial_number()
        deck_stack = gl.app.main_win.leftArea.deck_stack
        deck_stack_page = deck_stack.get_child_by_name(serial_number)
        if deck_stack_page == None:
            return
        return deck_stack_page.page_settings.grid_page
    
    def clear_media_player_tasks(self):
        self.media_player_tasks = {}


class Background:
    def __init__(self, deck_controller: DeckController):
        self.deck_controller = deck_controller

        self.image = None
        self.video = None

        self.tiles: list[Image.Image] = [None] * deck_controller.deck.key_count()

    def set_image(self, image: "BackgroundImage", update: bool = True) -> None:
        self.image = image
        self.video = None

        self.update_tiles()
        if update:
            self.deck_controller.update_all_keys()

    def set_video(self, video: "BackgroundVideo", update: bool = True) -> None:
        self.image = None
        self.video = video

        self.update_tiles()
        if update:
            self.deck_controller.update_all_keys()

    def set_from_path(self, path: str, update: bool = True) -> None:
        if path == "":
            path = None
        if path is None:
            self.image = None
            self.video = None
            self.update_tiles()
            if update:
                self.deck_controller.update_all_keys()
        elif is_video(path):
            self.set_video(BackgroundVideo(self.deck_controller, path), update=update)
        else:
            self.set_image(BackgroundImage(self.deck_controller, Image.open(path)), update=update)

    def update_tiles(self) -> None:
        if self.image is not None:
            self.tiles = self.image.get_tiles()
        elif self.video is not None:
            self.tiles = self.video.get_next_tiles()
        else:
            self.tiles = [self.deck_controller.generate_alpha_key() for _ in range(self.deck_controller.deck.key_count())]

        

class BackgroundImage:
    def __init__(self, deck_controller: DeckController, image: Image) -> None:
        self.deck_controller = deck_controller
        self.image = image

    def create_full_deck_sized_image(self) -> Image:
        key_rows, key_cols = self.deck_controller.deck.key_layout()
        key_width, key_height = self.deck_controller.get_key_image_size()
        spacing_x, spacing_y = 36, 36

        key_width *= key_cols
        key_height *= key_rows

        # Compute the total number of extra non-visible pixels that are obscured by
        # the bezel of the StreamDeck.
        spacing_x *= key_cols - 1
        spacing_y *= key_rows - 1

        # Compute final full deck image size, based on the number of buttons and
        # obscured pixels.
        full_deck_image_size = (key_width + spacing_x, key_height + spacing_y)

        # Resize the image to suit the StreamDeck's full image size. We use the
        # helper function in Pillow's ImageOps module so that the image's aspect
        # ratio is preserved.
        return ImageOps.fit(self.image, full_deck_image_size, Image.LANCZOS)
    
    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key):
        key_spacing = (36, 36)
        deck = self.deck_controller.deck


        key_rows, key_cols = deck.key_layout()
        key_width, key_height = deck.key_image_format()['size']
        spacing_x, spacing_y = key_spacing

        # Determine which row and column the requested key is located on.
        row = key // key_cols
        col = key % key_cols

        # Compute the starting X and Y offsets into the full size image that the
        # requested key should display.
        start_x = col * (key_width + spacing_x)
        start_y = row * (key_height + spacing_y)

        # Compute the region of the larger deck image that is occupied by the given
        # key, and crop out that segment of the full image.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        segment = image.crop(region)

        # Create a new key-sized image, and paste in the cropped section of the
        # larger image.
        key_image = PILHelper.create_key_image(deck)
        key_image.paste(segment)

        return key_image
    
    def get_tiles(self) -> list[Image.Image]:
        full_deck_sized_image = self.create_full_deck_sized_image()

        tiles: list[Image.Image] = []
        for key in range(self.deck_controller.deck.key_count()):
            key_image = self.crop_key_image_from_deck_sized_image(full_deck_sized_image, key)
            tiles.append(key_image)

        return tiles


class BackgroundVideo(BackgroundVideoCache):
    def __init__(self, deck_controller: DeckController, video_path: str):
        self.deck_controller = deck_controller
        self.video_path = video_path

        self.active_frame: int = -1

        super().__init__(video_path)

    def get_next_tiles(self) -> list[Image.Image]:
        self.active_frame += 1

        if self.active_frame >= self.n_frames:
            self.active_frame = 0

        return self.get_tiles(self.active_frame)

        frame = self.get_next_frame()
        frame_full_sized_image = self.create_full_deck_sized_image(frame)

        tiles: list[Image.Image] = []
        for key in range(self.deck_controller.deck.key_count()):
            key_image = self.crop_key_image_from_deck_sized_image(frame_full_sized_image, key)
            tiles.append(key_image)

        return tiles

    def get_next_frame(self) -> Image.Image:
        self.active_frame += 1

        if self.active_frame >= self.n_frames:
            self.active_frame = 0
        
        return self.get_frame(self.active_frame)
    
    def create_full_deck_sized_image(self, frame: Image.Image) -> Image.Image:
        key_rows, key_cols = self.deck_controller.deck.key_layout()
        key_width, key_height = self.deck_controller.get_key_image_size()
        spacing_x, spacing_y = 36, 36

        key_width *= key_cols
        key_height *= key_rows

        # Compute the total number of extra non-visible pixels that are obscured by
        # the bezel of the StreamDeck.
        spacing_x *= key_cols - 1
        spacing_y *= key_rows - 1

        # Compute final full deck image size, based on the number of buttons and
        # obscured pixels.
        full_deck_image_size = (key_width + spacing_x, key_height + spacing_y)

        # Resize the image to suit the StreamDeck's full image size. We use the
        # helper function in Pillow's ImageOps module so that the image's aspect
        # ratio is preserved.
        return ImageOps.fit(frame, full_deck_image_size, Image.Resampling.HAMMING)
    
    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key):
        key_spacing = (36, 36)
        deck = self.deck_controller.deck


        key_rows, key_cols = deck.key_layout()
        key_width, key_height = deck.key_image_format()['size']
        spacing_x, spacing_y = key_spacing

        # Determine which row and column the requested key is located on.
        row = key // key_cols
        col = key % key_cols

        # Compute the starting X and Y offsets into the full size image that the
        # requested key should display.
        start_x = col * (key_width + spacing_x)
        start_y = row * (key_height + spacing_y)

        # Compute the region of the larger deck image that is occupied by the given
        # key, and crop out that segment of the full image.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        segment = image.crop(region)

        # Create a new key-sized image, and paste in the cropped section of the
        # larger image.
        key_image = PILHelper.create_key_image(deck)
        key_image.paste(segment)

        return key_image




class ControllerKey:
    def __init__(self, deck_controller: DeckController, key: int):
        self.deck_controller = deck_controller
        self.key = key

        self.image_margins = [0, 0, 0, 0] # left, top, right, bottom

        self.labels: dict = {}

        self.key_image: KeyImage = None
        self.key_video: KeyVideo = None

        self.hide_error_timer: Timer = None

    def get_current_deck_image(self) -> Image.Image:
        foreground = None

        if self.key_image is not None:
            foreground = self.key_image.get_composite_image()
        elif self.key_video is not None:
            foreground = self.key_video.get_next_frame()

        if foreground is None:
            foreground = self.deck_controller.generate_alpha_key()

        background = copy(self.deck_controller.background.tiles[self.key])


        if background is None:
            background = self.deck_controller.generate_alpha_key().copy()

        
        if foreground.mode == "RGBA":
            background.paste(foreground, (0, 0), foreground)
        else:
            background.paste(foreground, (0, 0))

        labeled_image = self.add_labels_to_image(background)

        if self.is_pressed():
            labeled_image = self.shrink_image(labeled_image)

        return labeled_image
    
    def update(self) -> None:
        self.deck_controller.update_key(self.key)

    def set_key_image(self, key_image: "KeyImage", update: bool = True) -> None:
        self.key_image = key_image
        self.key_video = None

        if update:
            self.update()

    def set_key_video(self, key_video: "KeyVideo") -> None:
        self.key_video = key_video
        self.key_image = None

    def add_label(self, key_label: "KeyLabel", position: str = "center", update: bool = True) -> None:
        if position not in ["top", "center", "bottom"]:
            raise ValueError("Position must be one of 'top', 'center', or 'bottom'.")
        
        self.labels[position] = key_label

        if update:
            self.update()

    def remove_label(self, position: str = "center", update: bool = True) -> None:
        if position not in ["top", "center", "bottom"]:
            raise ValueError("Position must be one of 'top', 'center', or 'bottom'.")
        if position not in self.labels:
            return
        del self.labels[position]

        if update:
            self.update()

    def add_labels_to_image(self, image: Image.Image) -> Image.Image:
        image = image.copy()

        draw = ImageDraw.Draw(image)

        for label in self.labels:
            text = self.labels[label].text
            font_path = self.labels[label].get_font_path()
            color = tuple(self.labels[label].color)
            font_size = self.labels[label].font_size
            font = ImageFont.truetype(font_path, font_size)
            font_weight = self.labels[label].font_weight

            if text is None:
                continue
            
            if label == "top":
                position = (image.width / 2, font_size - 3)

            if label == "center":
                position = (image.width / 2, (image.height + font_size) / 2 - 3)

            if label == "bottom":
                position = (image.width / 2, image.height - 3)

            draw.text(position,
                        text=text, font=font, anchor="ms",
                        fill=color, stroke_width=font_weight)

        return image
    
    def is_pressed(self) -> bool:
        return self.deck_controller.deck.key_states()[self.key]
    
    def add_border(self, image: Image.Image) -> Image.Image:
        image = image.copy()
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((-1, -1, image.width, image.height), fill=None, outline=(255, 105, 0), width=8, radius=8)

        return image
    
    def shrink_image(self, image: Image.Image, factor: float = 0.7) -> Image.Image:
        image = image.copy()
        width = int(image.width * factor)
        height = int(image.height * factor)
        image = image.resize((width, height))

        background = Image.new("RGBA", self.deck_controller.get_key_image_size(), (0, 0, 0, 0))

        background.paste(image, (int((self.deck_controller.get_key_image_size()[0] - width) / 2), int((self.deck_controller.get_key_image_size()[1] - height) / 2)))

        return background
    
    def show_error(self, duration: int = -1):
        """
        duration: -1 for infinite
        """
        if duration == 0:
            self.stop_error_timer()
        elif duration > 0:
            self.hide_error_timer = Timer(duration, self.hide_error, args=[self.key_image, self.key_video, self.labels])
            self.hide_error_timer.start()

        with Image.open(os.path.join("Assets", "images", "error.png")) as image:
            image = image.copy()

        new_key_image = KeyImage(
            controller_key=self,
            image=image,
            margins=[10, 10, 10, 10]
        )

        self.set_key_image(new_key_image)

    def hide_error(self, original_key_image: "KeyImage", original_video: "KeyVideo", original_labels: dict = {}):
        self.labels = original_labels
        
        if original_video is not None:
            self.set_key_video(original_video) # This also applies the labels
        if original_key_image is not None:
            self.set_key_image(original_key_image) # This also applies the labels

    def stop_error_timer(self):
        if self.hide_error_timer is not None:
            self.hide_error_timer.cancel()
            self.hide_error_timer = None

    def load_from_page_dict(self, page_dict, update: bool = True, load_labels: bool = True, load_media: bool = True):
        if page_dict in [None, {}]:
            self.clear(update=update)
            return
                
        self.own_actions_ready()

        ## Load labels
        if load_labels:
            self.labels = {}

            for label in page_dict.get("labels", []):
                key_label = KeyLabel(
                    controller_key=self,
                    text=page_dict["labels"][label].get("text"),
                    font_size=page_dict["labels"][label].get("font-size"),
                    font_name=page_dict["labels"][label].get("font-family"),
                    color=page_dict["labels"][label].get("color"),
                    font_weight=page_dict["labels"][label].get("stroke-width")
                )
                self.add_label(key_label, position=label, update=False)


        ## Load media
        if load_media:
            self.key_image = None
            self.key_video = None

            path = page_dict.get("media", {}).get("path", None)
            if path not in ["", None]:
                print(f"media on key {self.key} is {path}")
                if is_image(path):
                    self.set_key_image(KeyImage(
                        controller_key=self,
                        image=Image.open(path),
                        fill_mode=page_dict.get("media", {}).get("fill-mode", "cover"),
                        margins=page_dict.get("media", {}).get("margins", [0, 0, 0, 0])
                    ), update=False)

                elif is_video(path) and True:
                    self.set_key_video(KeyVideo(
                        controller_key=self,
                        video_path=path,
                    )) # Videos always update


        if update:
            self.update()

    def clear(self, update: bool = True):
        self.key_image = None
        self.key_video = None
        self.labels = {}
        if update:
            self.update()

    def set_ui_key_image(self, image: Image.Image) -> None:
        if image is None:
            return
        
        x, y = self.deck_controller.index_to_coords(self.key)
        
        if self.deck_controller.get_own_key_grid() is None:
            # Save to use later
            self.deck_controller.ui_grid_buttons_changes_while_hidden[(y, x)] = image # The ui key coords are in reverse order
        else:
            # self.get_own_key_grid().buttons[y][x].set_image(pixbuf)
            GLib.idle_add(self.deck_controller.get_own_key_grid().buttons[y][x].set_image, image)
        
    def get_own_ui_key(self) -> KeyButton:
        x, y = self.deck_controller.index_to_coords(self.key)
        buttons = self.deck_controller.get_own_key_grid().buttons # The ui key coords are in reverse order
        return buttons[x][y]
    
    def get_own_actions(self) -> list["ActionBase"]:
        active_page = self.deck_controller.active_page
        own_coords = self.deck_controller.index_to_coords(self.key)
        page_coords = f"{own_coords[0]}x{own_coords[1]}"
        if page_coords == "0x2":
            print()

        actions = list(active_page.action_objects.get(page_coords, {}).values())
        return actions
    
    def on_key_change(self, state) -> None:
        self.update()

        if state:
            self.own_actions_key_down()
        else:
            self.own_actions_key_up()


    def own_actions_ready(self) -> None:
        for action in self.get_own_actions():
            action.on_ready()
            
        if self.key_image is not None:
            print()

    def own_actions_key_down(self) -> None:
        for action in self.get_own_actions():
            action.on_key_down()

    def own_actions_key_up(self) -> None:
        for action in self.get_own_actions():
            action.on_key_up()

    def own_actions_tick(self) -> None:
        for action in self.get_own_actions():
            action.on_tick()


class KeyLabel:
    def __init__(self, controller_key: ControllerKey, text: str, font_size: int = 16, font_name: str = None, color: list[int] = [255, 255, 255, 255], font_weight: int = 1):
        self.controller_key = controller_key
        self.text = text
        self.font_size = font_size
        self.font_name = font_name
        self.color = color
        self.font_weight = font_weight

    def get_font_path(self) -> str:
        return matplotlib.font_manager.findfont(matplotlib.font_manager.FontProperties(family=self.font_name))


class KeyImage:
    def __init__(self, controller_key: ControllerKey, image: Image, fill_mode: str = "cover", margins: list[int] = [0, 0, 0, 0]):
        self.controller_key = controller_key
        self.image: Image = image
        self.fill_mode = fill_mode
        self.margins = margins


    def get_composite_image(self, background: Image = None) -> Image:
        if background is None:
            background = self.controller_key.deck_controller.generate_alpha_key()

        # Calculate the box where the inner image should be fitted
        box = (self.margins[3], self.margins[0], background.width - self.margins[1], background.height - self.margins[2])
        box_size = (box[2] - box[0], box[3] - box[1])


        if self.fill_mode == "stretch":
            image_size = [background.width - self.margins[0] - self.margins[2], background.height - self.margins[1] - self.margins[3]]
            image_resized = self.image.resize(image_size, Image.Resampling.HAMMING)

        elif self.fill_mode == "cover":
            image_resized = ImageOps.cover(self.image, box_size, Image.Resampling.HAMMING)

        elif self.fill_mode == "contain":
            image_resized = ImageOps.contain(self.image, box_size, Image.Resampling.HAMMING)
        
        else:
            raise ValueError(f"Unknown fill mode: {self.fill_mode}")

        background.paste(image_resized, self.margins[0:2])

        return background
    

class KeyVideo(VideoFrameCache):
    def __init__(self, controller_key: ControllerKey, video_path: str, fill_mode: str = "cover", margins: list[int] = [0, 0, 0, 0]):
        self.controller_key = controller_key
        self.video_path = video_path
        self.fill_mode = fill_mode
        self.margins = margins

        self.active_frame: int = -1

        super().__init__(video_path)


    def get_next_frame(self) -> Image:
        self.active_frame += 1

        if self.active_frame >= self.n_frames:
            self.active_frame = 0
        
        return self.get_frame(self.active_frame).resize(self.controller_key.deck_controller.get_key_image_size(), Image.Resampling.LANCZOS)

        frame = self.frames[self.active_frame]
        print(type(frame))
        return frame