import asyncio
import ctypes.wintypes
from functools import cached_property
from typing import List

import pymem

from . import Keycode, NotInCombat, ReadingEnumFailed, utils
from .memory import (
    DuelPhase,
    HookHandler,
    PlayerStats,
    PlayerActorBody,
    PlayerDuel,
    CurrentQuestPosition,
)

from .constants import user32, WIZARD_SPEED
from .utils import XYZ, check_if_process_running
from .combat import Card


class Client:
    """
    Represents a connected wizard client

    Args:
        window_handle: A handle to the window this client connects to
    """

    def __init__(self, window_handle: int):
        self.window_handle = window_handle

        self._pymem = pymem.Pymem()
        self._pymem.open_process_from_id(self.process_id)
        self.hook_handler = HookHandler(self._pymem)

        self.click_lock = None

    def __repr__(self):
        return f"<Client {self.window_handle=} {self.process_id=}>"

    def is_running(self):
        return check_if_process_running(self._pymem.process_handle)

    async def close(self):
        """
        Closes this client; unhooking all active hooks
        """
        # if the client isn't running there isn't anything to unhook
        if not self.is_running():
            return

        await self.hook_handler.close()

    @cached_property
    def process_id(self) -> int:
        """
        Client's process id
        """
        return utils.get_pid_from_handle(self.window_handle)

    @cached_property
    def stats(self) -> PlayerStats:
        """
        Client's game stats struct
        """
        return PlayerStats(self.hook_handler)

    @cached_property
    def body(self) -> PlayerActorBody:
        """
        Client's actor body struct
        """
        return PlayerActorBody(self.hook_handler)

    @cached_property
    def duel(self) -> PlayerDuel:
        """
        Client's duel struct
        """
        return PlayerDuel(self.hook_handler)

    @cached_property
    def quest_position(self) -> CurrentQuestPosition:
        """
        Client's quest position struct
        """
        return CurrentQuestPosition(self.hook_handler)

    # async def get_cards(self) -> List[Card]:
    #     """
    #     Get the client's current cards
    #     """
    #     if not await self.in_battle():
    #         raise NotInCombat("Must be in combat to get cards")
    #
    #     spells = []

    async def in_battle(self) -> bool:
        """
        If the client is in battle or not
        """
        try:
            duel_phase = await self.duel.duel_phase()
        except ReadingEnumFailed:
            return False
        else:
            return duel_phase is not DuelPhase.ended

    async def activate_hooks(self):
        """
        Activate all memory hooks
        """
        await self.hook_handler.activate_all_hooks()

    def login(self, username: str, password: str):
        """
        Login to a client that is at the login screen

        Args:
            username: The username to login with
            password: The password to login with
        """
        utils.instance_login(self.window_handle, username, password)

    async def send_key(self, key: Keycode, seconds: float = 0.5):
        await utils.timed_send_key(self.window_handle, key, seconds)

    async def click(
        self,
        x: int,
        y: int,
        *,
        right_click: bool = False,
        sleep_duration: float = 0.0,
        use_post: bool = False,
    ):
        """
        Send a click to a certain x and y
        x and y positions are relative to the top left corner of the screen

        Args:
            x: x to click at
            y: y to click at
            right_click: If the click should be a right click
            sleep_duration: How long to sleep between messages
            use_post: If PostMessage should be used instead of SendMessage
        """
        # prevent multiple clicks from happening at the same time
        if right_click:
            button_down_message = 0x204
        else:
            button_down_message = 0x201

        if use_post:
            send_method = user32.PostMessageW
        else:
            send_method = user32.SendMessageW

        if self.click_lock is None:
            self.click_lock = asyncio.Lock()

        async with self.click_lock:
            # TODO: test passing use_post
            await self.set_mouse_position(x, y)
            # mouse button down
            send_method(self.window_handle, button_down_message, 1, 0)
            if sleep_duration > 0:
                await asyncio.sleep(sleep_duration)
            # mouse button up
            send_method(self.window_handle, button_down_message + 1, 0, 0)

    async def set_mouse_position(
        self,
        x: int,
        y: int,
        *,
        convert_from_client: bool = True,
        use_post: bool = False,
    ):
        """
        Set's the mouse position to a certain x y relative to the
        top left corner of the client

        Args:
            x: x to set
            y: y to set
            convert_from_client: If the position should be converted from client to screen
            use_post: If PostMessage should be used instead of SendMessage
        """
        if use_post:
            send_method = user32.PostMessageW
        else:
            send_method = user32.SendMessageW

        if convert_from_client:
            point = ctypes.wintypes.tagPOINT(x, y)

            # https://docs.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-clienttoscreen
            if user32.ClientToScreen(self.window_handle, ctypes.byref(point)) == 0:
                raise RuntimeError("Client to screen conversion failed")

            # same point structure is overwritten by ClientToScreen; these are also ints and not
            # c_longs for some reason?
            x = point.x
            y = point.y

        res = await self.hook_handler.write_mouse_position(x, y)
        # position doesn't matter here; sending mouse move
        # mouse move is here so that items are highlighted
        send_method(self.window_handle, 0x200, 0, 0)
        return res

    async def goto(
        self, x: float, y: float, *, speed_multiplier: float = 1.0,
    ):
        """
        Moves the player to a specific x and y

        Args:
            x: X to move to
            y: Y to move to
            speed_multiplier: Multiplier for speed (for mounts) i.e. 1.4 for 40%
        """
        current_xyz = await self.body.position()
        target_xyz = utils.XYZ(x, y, current_xyz.z)
        distance = current_xyz - target_xyz
        move_seconds = distance / (WIZARD_SPEED * speed_multiplier)
        yaw = utils.calculate_perfect_yaw(current_xyz, target_xyz)

        await self.body.write_yaw(yaw)
        await utils.timed_send_key(self.window_handle, Keycode.W, move_seconds)

    async def teleport(self, xyz: XYZ, yaw: int = None):
        """
        Teleport the client

        Args:
            xyz: xyz to teleport to
            yaw: yaw to set or None to not change

        Raises:
            RuntimeError: player hook not active
        """
        await self.body.write_position(xyz)

        if yaw is not None:
            await self.body.write_yaw(yaw)
