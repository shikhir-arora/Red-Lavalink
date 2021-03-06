from collections import namedtuple
from enum import Enum
import asyncio
import json
from typing import Dict, List

import websockets
from discord.backoff import ExponentialBackoff

from . import log


__all__ = ['DiscordVoiceSocketResponses', 'LavalinkEvents',
           'LavalinkOutgoingOp', 'get_node', 'join_voice']

SHUTDOWN = asyncio.Event()
_nodes = {}  # type: Dict[Node, List[int]]


class DiscordVoiceSocketResponses(Enum):
    VOICE_STATE_UPDATE = 'VOICE_STATE_UPDATE'
    VOICE_SERVER_UPDATE = 'VOICE_SERVER_UPDATE'


class LavalinkIncomingOp(Enum):
    EVENT = 'event'
    PLAYER_UPDATE = 'playerUpdate'
    STATS = 'stats'


class LavalinkOutgoingOp(Enum):
    VOICE_UPDATE = 'voiceUpdate'
    PLAY = 'play'
    STOP = 'stop'
    PAUSE = 'pause'
    SEEK = 'seek'
    VOLUME = 'volume'


class LavalinkEvents(Enum):
    """
    An enumeration of the Lavalink Track Events.

    Attributes
    ----------
    TRACK_END
        The track playback has ended.
    TRACK_EXCEPTION
        There was an exception during track playback.
    TRACK_STUCK
        Track playback got stuck during playback.
    """
    TRACK_END = 'TrackEndEvent'
    TRACK_EXCEPTION = 'TrackExceptionEvent'
    TRACK_STUCK = 'TrackStuckEvent'


PlayerState = namedtuple('PlayerState', 'position time')
MemoryInfo = namedtuple('MemoryInfo', 'reservable used free allocated')
CPUInfo = namedtuple('CPUInfo', 'cores systemLoad lavalinkLoad')



class Stats:
    def __init__(self, memory, players, active_players, cpu, uptime):
        self.memory = MemoryInfo(**memory)
        self.players = players
        self.active_players = active_players
        self.cpu_info = CPUInfo(**cpu)
        self.uptime = uptime


class Node:
    def __init__(self, _loop, event_handler, voice_ws_func,
                 host, password, port, user_id, num_shards):
        """

        Parameters
        ----------
        _loop : asyncio.BaseEventLoop
            The event loop of the bot.
        event_handler
            Function to dispatch events to.
        voice_ws_func : typing.Callable
            Function that takes one argument, guild ID, and returns a websocket.
        host : str
            Lavalink player host.
        password : str
            Password for the Lavalink player.
        port : int
            Port of the Lavalink player event websocket.
        user_id : int
            User ID of the bot.
        num_shards : int
            Number of shards to which the bot is currently connected.
        """
        self.loop = _loop
        self.event_handler = event_handler
        self.voice_ws_func = voice_ws_func
        self.host = host
        self.port = port
        self.headers = self._get_connect_headers(password, user_id, num_shards)

        self._ws = None
        self._listener_task = None

        self._queue = []

        _nodes[self] = []

    async def connect(self, timeout=None):
        """
        Connects to the Lavalink player event websocket.

        Parameters
        ----------
        timeout : int
            Time after which to timeout on attempting to connect to the Lavalink websocket,
            ``None`` is considered never.

        Raises
        ------
        asyncio.TimeoutError
            If the websocket failed to connect after the given time.
        """
        SHUTDOWN.clear()

        uri = "ws://{}:{}".format(self.host, self.port)

        log.debug('Lavalink WS connecting to {} with headers {}'.format(
            uri, self.headers
        ))

        await asyncio.wait_for(
            self._multi_try_connect(uri),
            timeout=timeout
        )

        log.debug('Creating Lavalink WS listener.')
        self._listener_task = self.loop.create_task(self.listener())

        for data in self._queue:
            await self.send(data)

    @staticmethod
    def _get_connect_headers(password, user_id, num_shards):
        return {
            'Authorization': password,
            'User-Id': user_id,
            'Num-Shards': num_shards
        }

    async def _multi_try_connect(self, uri):
        backoff = ExponentialBackoff()
        attempt = 1
        while not SHUTDOWN.is_set() and (self._ws is None or not self._ws.open):
            try:
                self._ws = await websockets.connect(uri, extra_headers=self.headers)
            except OSError:
                delay = backoff.delay()
                log.debug("Failed connect attempt {}, retrying in {}".format(
                    attempt, delay
                ))
                await asyncio.sleep(delay)
                attempt += 1

    async def listener(self):
        """
        Listener task for receiving ops from Lavalink.
        """
        while self._ws.open and not SHUTDOWN.is_set():
            try:
                data = json.loads(await self._ws.recv())
            except websockets.ConnectionClosed:
                break

            raw_op = data.get('op')
            try:
                op = LavalinkIncomingOp(raw_op)
            except ValueError:
                log.debug("Received unknown op: {}".format(data))
            else:
                log.debug("Received known op: {}".format(data))
                self.loop.create_task(self._handle_op(op, data))

        log.debug('Listener exited: ws {} SHUTDOWN {}.'.format(
            self._ws.open, SHUTDOWN.is_set()
        ))
        self.loop.create_task(self._reconnect())

    async def _handle_op(self, op: LavalinkIncomingOp, data):
        if op == LavalinkIncomingOp.EVENT:
            try:
                event = LavalinkEvents(data.get('type'))
            except ValueError:
                log.debug("Unknown event type: {}".format(data))
            else:
                self.event_handler(op, event, data)
        elif op == LavalinkIncomingOp.PLAYER_UPDATE:
            state = PlayerState(**data.get('state'))
            self.event_handler(op, state, data)
        elif op == LavalinkIncomingOp.STATS:
            stats = Stats(
                memory=data.get('memory'),
                players=data.get('players'),
                active_players=data.get('playingPlayers'),
                cpu=data.get('cpu'),
                uptime=data.get('uptime')
            )
            self.event_handler(op, stats, data)

    async def _reconnect(self):
        if SHUTDOWN.is_set():
            log.debug('Shutting down Lavalink WS.')
            return

        log.debug("Attempting Lavalink WS reconnect.")
        try:
            await self.connect()
        except asyncio.TimeoutError:
            log.debug("Failed to reconnect, please reinitialize lavalink when ready.")
        else:
            log.debug("Reconnect successful.")

    async def disconnect(self):
        """
        Shuts down and disconnects the websocket.
        """
        SHUTDOWN.set()
        await self._ws.close()
        del _nodes[self]
        log.debug("Shutdown Lavalink WS.")

    async def send(self, data):
        if self._ws is None or not self._ws.open:
            self._queue.append(data)
        else:
            log.debug("Sending data to Lavalink: {}".format(data))
            await self._ws.send(json.dumps(data))

    async def send_lavalink_voice_update(self, guild_id, session_id, event):
        await self.send({
            'op': LavalinkOutgoingOp.VOICE_UPDATE.value,
            'guildId': str(guild_id),
            'sessionId': session_id,
            'event': event
        })

    # Player commands
    async def stop(self, guild_id: int):
        await self.send({
            'op': LavalinkOutgoingOp.STOP.value,
            'guildId': str(guild_id)
        })

    async def play(self, guild_id: int, track_identifier: str):
        await self.send({
            'op': LavalinkOutgoingOp.PLAY.value,
            'guildId': str(guild_id),
            'track': track_identifier
        })

    async def pause(self, guild_id, paused):
        await self.send({
            'op': LavalinkOutgoingOp.PAUSE.value,
            'guildId': str(guild_id),
            'pause': paused
        })

    async def volume(self, guild_id: int, _volume: int):
        await self.send({
            'op': LavalinkOutgoingOp.VOLUME.value,
            'guildId': str(guild_id),
            'volume': _volume
        })

    async def seek(self, guild_id: int, position: int):
        await self.send({
            'op': LavalinkOutgoingOp.SEEK.value,
            'guildId': str(guild_id),
            'position': position
        })


def get_node(guild_id: int) -> Node:
    """
    Gets a node based on a guild ID, useful for noding separation. If the
    guild ID does not already have a node association, the least used
    node is returned.

    Parameters
    ----------
    guild_id : int

    Returns
    -------
    Node
    """
    guild_count = 1e10
    least_used = None
    for node, guild_ids in _nodes.items():
        if len(guild_ids) < guild_count:
            guild_count = len(guild_ids)
            least_used = node

        if guild_id in guild_ids:
            return node

    _nodes[least_used].append(guild_id)
    return least_used


async def join_voice(guild_id: int, channel_id: int):
    """
    Joins a voice channel by ID's.

    Parameters
    ----------
    guild_id : int
    channel_id : int
    """
    node = get_node(guild_id)
    voice_ws = node.voice_ws_func(guild_id)
    await voice_ws.voice_state(guild_id, channel_id)


async def disconnect():
    nodes = list(_nodes.keys())
    for node in nodes:
        await node.disconnect()
