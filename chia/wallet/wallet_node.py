import asyncio
import json
import logging
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Any

from blspy import PrivateKey, AugSchemeMPL
from packaging.version import Version

from chia.consensus.block_record import BlockRecord
from chia.consensus.blockchain import ReceiveBlockResult
from chia.consensus.constants import ConsensusConstants
from chia.daemon.keychain_proxy import (
    KeychainProxyConnectionFailure,
    connect_to_keychain_and_validate,
    wrap_local_keychain,
    KeychainProxy,
    KeyringIsEmpty,
)
from chia.full_node.lock_queue import LockQueue, LockClient
from chia.full_node.weight_proof import chunks
from chia.protocols import wallet_protocol
from chia.protocols.full_node_protocol import RequestProofOfWeight, RespondProofOfWeight
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.protocols.wallet_protocol import (
    RespondToCoinUpdates,
    CoinState,
    RespondToPhUpdates,
    RespondBlockHeader,
    RequestSESInfo,
    RespondSESInfo,
    RequestHeaderBlocks,
    RespondHeaderBlocks,
)
from chia.server.node_discovery import WalletPeers
from chia.server.outbound_message import Message, NodeType, make_msg
from chia.server.peer_store_resolver import PeerStoreResolver
from chia.server.server import ChiaServer
from chia.server.ws_connection import WSChiaConnection
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.blockchain_format.sub_epoch_summary import SubEpochSummary
from chia.types.coin_spend import CoinSpend
from chia.types.header_block import HeaderBlock
from chia.types.mempool_inclusion_status import MempoolInclusionStatus
from chia.types.peer_info import PeerInfo
from chia.types.weight_proof import WeightProof, SubEpochData
from chia.util.byte_types import hexstr_to_bytes
from chia.util.config import WALLET_PEERS_PATH_KEY_DEPRECATED
from chia.util.default_root import STANDALONE_ROOT_PATH
from chia.util.ints import uint32, uint64
from chia.util.keychain import KeyringIsLocked, Keychain
from chia.util.path import mkdir, path_from_root
from chia.wallet.util.filter_coin_states import filter_coin_states
from chia.wallet.util.wallet_sync_utils import (
    request_and_validate_removals,
    request_and_validate_additions,
    can_use_peer_request_cache,
    PeerRequestCache,
    fetch_last_tx_from_peer,
)
from chia.wallet.wallet_coin_record import WalletCoinRecord
from chia.wallet.wallet_state_manager import WalletStateManager
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.wallet_action import WalletAction
from chia.util.profiler import profile_task


class WalletNode:
    key_config: Dict
    config: Dict
    constants: ConsensusConstants
    server: Optional[ChiaServer]
    log: logging.Logger
    # Maintains the state of the wallet (blockchain and transactions), handles DB connections
    wallet_state_manager: Optional[WalletStateManager]
    _shut_down: bool
    root_path: Path
    state_changed_callback: Optional[Callable]
    syncing: bool
    full_node_peer: Optional[PeerInfo]
    peer_task: Optional[asyncio.Task]
    logged_in: bool
    wallet_peers_initialized: bool
    keychain_proxy: Optional[KeychainProxy]
    wallet_peers: Optional[WalletPeers]
    race_cache: Dict[bytes32, Set[CoinState]]
    race_cache_hashes: List[Tuple[uint32, bytes32]]
    _new_peak_lock: asyncio.Lock
    _new_peak_lock_queue: LockQueue
    _new_peak_lock_ultra_priority: LockClient
    _new_peak_lock_high_priority: LockClient
    _new_peak_lock_low_priority: LockClient
    subscription_queue: asyncio.Queue
    _process_new_subscriptions_task: Optional[asyncio.Task]
    _node_peaks: Dict[bytes32, Tuple[uint32, bytes32]]
    validation_semaphore: Optional[asyncio.Semaphore]
    local_node_synced: bool
    new_state_lock: Optional[asyncio.Lock]

    def __init__(
        self,
        config: Dict,
        root_path: Path,
        consensus_constants: ConsensusConstants,
        name: str = None,
        local_keychain: Optional[Keychain] = None,
    ):
        self.config = config
        self.constants = consensus_constants
        self.root_path = root_path
        self.log = logging.getLogger(name if name else __name__)
        # Normal operation data
        self.cached_blocks: Dict = {}
        self.future_block_hashes: Dict = {}

        # Sync data
        self._shut_down = False
        self.proof_hashes: List = []
        self.state_changed_callback = None
        self.wallet_state_manager = None
        self.new_state_lock = None
        self.server = None
        self.wsm_close_task = None
        self.sync_task: Optional[asyncio.Task] = None
        self.logged_in_fingerprint: Optional[int] = None
        self.peer_task = None
        self.logged_in = False
        self.keychain_proxy = None
        self.local_keychain = local_keychain
        self.height_to_time: Dict[uint32, uint64] = {}
        self.synced_peers: Set[bytes32] = set()  # Peers that we have long synced to
        self.wallet_peers = None
        self.wallet_peers_initialized = False
        self.valid_wp_cache: Dict[bytes32, Any] = {}
        self.untrusted_caches: Dict[bytes32, Any] = {}
        self.race_cache = {}  # in Untrusted mode wallet might get the state update before receiving the block
        self.race_cache_hashes = []
        self._process_new_subscriptions_task = None
        self._node_peaks = {}
        self.validation_semaphore = None
        self.local_node_synced = False
        self.LONG_SYNC_THRESHOLD = 200

    async def ensure_keychain_proxy(self) -> KeychainProxy:
        if not self.keychain_proxy:
            if self.local_keychain:
                self.keychain_proxy = wrap_local_keychain(self.local_keychain, log=self.log)
            else:
                self.keychain_proxy = await connect_to_keychain_and_validate(self.root_path, self.log)
                if not self.keychain_proxy:
                    raise KeychainProxyConnectionFailure("Failed to connect to keychain service")
        return self.keychain_proxy

    def get_cache_for_peer(self, peer) -> PeerRequestCache:
        if peer.peer_node_id not in self.untrusted_caches:
            self.untrusted_caches[peer.peer_node_id] = PeerRequestCache()
        return self.untrusted_caches[peer.peer_node_id]

    def rollback_request_caches(self, reorg_height: int):
        # Everything after reorg_height should be removed from the cache
        for cache in self.untrusted_caches.values():
            cache.clear_after_height(reorg_height)

    async def get_key_for_fingerprint(self, fingerprint: Optional[int]) -> Optional[PrivateKey]:
        try:
            keychain_proxy = await self.ensure_keychain_proxy()
            key = await keychain_proxy.get_key_for_fingerprint(fingerprint)
        except KeyringIsEmpty:
            self.log.warning("No keys present. Create keys with the UI, or with the 'chia keys' program.")
            return None
        except KeyringIsLocked:
            self.log.warning("Keyring is locked")
            return None
        except KeychainProxyConnectionFailure as e:
            tb = traceback.format_exc()
            self.log.error(f"Missing keychain_proxy: {e} {tb}")
            raise e  # Re-raise so that the caller can decide whether to continue or abort
        return key

    async def _start(
        self,
        fingerprint: Optional[int] = None,
    ) -> bool:
        # Makes sure the coin_state_updates get higher priority than new_peak messages
        self._new_peak_lock = asyncio.Lock()
        # TODO: a limited max size can cause a problem, but too many tasks can also overload the node (do a queue)
        self._new_peak_lock_queue = LockQueue(self._new_peak_lock, maxsize=10 * self.LONG_SYNC_THRESHOLD)
        self._new_peak_lock_ultra_priority = LockClient(0, self._new_peak_lock_queue)
        self._new_peak_lock_high_priority = LockClient(1, self._new_peak_lock_queue)
        self._new_peak_lock_low_priority = LockClient(2, self._new_peak_lock_queue)
        self.subscription_queue = asyncio.Queue()

        self.synced_peers = set()
        private_key = await self.get_key_for_fingerprint(fingerprint)
        if private_key is None:
            self.logged_in = False
            return False

        if self.config.get("enable_profiler", False):
            asyncio.create_task(profile_task(self.root_path, "wallet", self.log))

        db_path_key_suffix = str(private_key.get_g1().get_fingerprint())
        db_path_replaced: str = (
            self.config["database_path"]
            .replace("CHALLENGE", self.config["selected_network"])
            .replace("KEY", db_path_key_suffix)
        )
        path = path_from_root(self.root_path, f"{db_path_replaced}_new")
        standalone_path = path_from_root(STANDALONE_ROOT_PATH, f"{db_path_replaced}_new")
        if not path.exists():
            if standalone_path.exists():
                path.write_bytes(standalone_path.read_bytes())

        mkdir(path.parent)
        assert self.server is not None
        self.wallet_state_manager = await WalletStateManager.create(
            private_key,
            self.config,
            path,
            self.constants,
            self.server,
            self.root_path,
            self,
        )

        assert self.wallet_state_manager is not None

        self.config["starting_height"] = 0

        if self.wallet_peers is None:
            self.initialize_wallet_peers()

        if self.state_changed_callback is not None:
            self.wallet_state_manager.set_callback(self.state_changed_callback)

        self.wallet_state_manager.set_pending_callback(self._pending_tx_handler)
        self._shut_down = False
        self._process_new_subscriptions_task = asyncio.create_task(self._process_new_subscriptions())

        self.sync_event = asyncio.Event()
        if fingerprint is None:
            self.logged_in_fingerprint = private_key.get_g1().get_fingerprint()
        else:
            self.logged_in_fingerprint = fingerprint
        self.logged_in = True
        self.wallet_state_manager.set_sync_mode(False)

        async with self.wallet_state_manager.puzzle_store.lock:
            index = await self.wallet_state_manager.puzzle_store.get_last_derivation_path()
            if index is None or index < self.config["initial_num_public_keys"] - 1:
                await self.wallet_state_manager.create_more_puzzle_hashes(from_zero=True)
                self.wsm_close_task = None
        return True

    def _close(self):
        self.log.info("self._close")
        self.logged_in_fingerprint = None
        self._shut_down = True

        if self._process_new_subscriptions_task is not None:
            self._process_new_subscriptions_task.cancel()
        self._new_peak_lock_queue.close()

    async def _await_closed(self):
        self.log.info("self._await_closed")

        if self.server is not None:
            await self.server.close_all_connections()
        if self.wallet_peers is not None:
            await self.wallet_peers.ensure_is_closed()
        if self.wallet_state_manager is not None:
            await self.wallet_state_manager._await_closed()
            self.wallet_state_manager = None
        await self._new_peak_lock_queue.await_closed()
        self.logged_in = False
        self.wallet_peers = None

    def _set_state_changed_callback(self, callback: Callable):
        self.state_changed_callback = callback

        if self.wallet_state_manager is not None:
            self.wallet_state_manager.set_callback(self.state_changed_callback)
            self.wallet_state_manager.set_pending_callback(self._pending_tx_handler)

    def _pending_tx_handler(self):
        if self.wallet_state_manager is None:
            return None
        asyncio.create_task(self._resend_queue())

    async def _action_messages(self) -> List[Message]:
        if self.wallet_state_manager is None:
            return []
        actions: List[WalletAction] = await self.wallet_state_manager.action_store.get_all_pending_actions()
        result: List[Message] = []
        for action in actions:
            data = json.loads(action.data)
            action_data = data["data"]["action_data"]
            if action.name == "request_puzzle_solution":
                coin_name = bytes32(hexstr_to_bytes(action_data["coin_name"]))
                height = uint32(action_data["height"])
                msg = make_msg(
                    ProtocolMessageTypes.request_puzzle_solution,
                    wallet_protocol.RequestPuzzleSolution(coin_name, height),
                )
                result.append(msg)

        return result

    async def _resend_queue(self):
        if self._shut_down or self.server is None or self.wallet_state_manager is None:
            return None

        for msg, sent_peers in await self._messages_to_resend():
            if self._shut_down or self.server is None or self.wallet_state_manager is None:
                return None
            full_nodes = self.server.get_full_node_connections()
            for peer in full_nodes:
                if peer.peer_node_id in sent_peers:
                    continue
                self.log.debug(f"sending: {msg}")
                await peer.send_message(msg)

        for msg in await self._action_messages():
            if self._shut_down or self.server is None or self.wallet_state_manager is None:
                return None
            await self.server.send_to_all([msg], NodeType.FULL_NODE)

    async def _messages_to_resend(self) -> List[Tuple[Message, Set[bytes32]]]:
        if self.wallet_state_manager is None or self._shut_down:
            return []
        messages: List[Tuple[Message, Set[bytes32]]] = []

        records: List[TransactionRecord] = await self.wallet_state_manager.tx_store.get_not_sent()

        for record in records:
            if record.spend_bundle is None:
                continue
            msg = make_msg(
                ProtocolMessageTypes.send_transaction,
                wallet_protocol.SendTransaction(record.spend_bundle),
            )
            already_sent = set()
            for peer, status, _ in record.sent_to:
                if status == MempoolInclusionStatus.SUCCESS.value:
                    already_sent.add(bytes32.from_hexstr(peer))
            messages.append((msg, already_sent))

        return messages

    async def _process_new_subscriptions(self):
        while not self._shut_down:
            try:
                # Here we get new subscription values and immediately subscribe to all connected nodes.
                # It is important that these get processed, so that users see all the coins that they own.
                sub_type, byte_values = await self.subscription_queue.get()

                # The purpose of locking with _new_peak_lock_ultra_priority is to ensure that we do not advance
                # the peak any further before adding the subscriptions that have to be added. This means that if the
                # node crashes, we will start from a peak that should be in the past of these subscriptions.
                async with self._new_peak_lock_ultra_priority:
                    for peer in self.server.get_full_node_connections():
                        if sub_type == 0:
                            self.log.debug(f"Processing new PH subscription: {byte_values}")
                            # Puzzle hash subscription
                            coin_states: List[CoinState] = await self.subscribe_to_phs(
                                byte_values, peer, True, uint32(0), None
                            )
                        elif sub_type == 1:
                            # Coin id subscription
                            self.log.debug(f"Processing new Coin subscription: {byte_values}")
                            coin_states: List[CoinState] = await self.subscribe_to_coin_updates(
                                byte_values, peer, True, uint32(0), None
                            )
                        else:
                            assert False
                        # Here we lock the wallet state manager because we might be changing state in the WSM
                        if len(coin_states) > 0:
                            async with self.wallet_state_manager.lock:
                                await self.receive_state_from_peer(coin_states, peer)
            except Exception as e:
                self.log.error(f"Got error {e} while processing subscriptions: {traceback.format_exc()}")

    def set_server(self, server: ChiaServer):
        self.server = server
        self.initialize_wallet_peers()

    def initialize_wallet_peers(self):
        self.server.on_connect = self.on_connect
        network_name = self.config["selected_network"]

        connect_to_unknown_peers = self.config.get("connect_to_unknown_peers", True)
        testing = self.config.get("testing", False)
        if connect_to_unknown_peers and not testing:
            self.wallet_peers = WalletPeers(
                self.server,
                self.config["target_peer_count"],
                PeerStoreResolver(
                    self.root_path,
                    self.config,
                    selected_network=network_name,
                    peers_file_path_key="wallet_peers_file_path",
                    legacy_peer_db_path_key=WALLET_PEERS_PATH_KEY_DEPRECATED,
                    default_peers_file_path="wallet/db/wallet_peers.dat",
                ),
                self.config["introducer_peer"],
                self.config.get("dns_servers", ["dns-introducer.chia.net"]),
                self.config["peer_connect_interval"],
                network_name,
                None,
                self.log,
            )
            await self.wallet_peers.start()

    def on_disconnect(self, peer: WSChiaConnection):
        if self.is_trusted(peer):
            self.local_node_synced = False

        if peer.peer_node_id in self.untrusted_caches:
            self.untrusted_caches.pop(peer.peer_node_id)
        if peer.peer_node_id in self.synced_peers:
            self.synced_peers.remove(peer.peer_node_id)
        if peer.peer_node_id in self._node_peaks:
            self._node_peaks.pop(peer.peer_node_id)

    async def on_connect(self, peer: WSChiaConnection):
        if self.wallet_state_manager is None:
            return None

        if Version(peer.protocol_version) < Version("0.0.33"):
            self.log.info("Disconnecting, full node running old software")
            await peer.close()

        trusted = self.is_trusted(peer)
        if not trusted and self.local_node_synced:
            await peer.close()

        if peer.peer_node_id in self.synced_peers:
            self.synced_peers.remove(peer.peer_node_id)

        self.log.info(f"Connected peer {peer.get_peer_info()} is trusted: {trusted}")
        messages_peer_ids = await self._messages_to_resend()
        self.wallet_state_manager.state_changed("add_connection")
        for msg, peer_ids in messages_peer_ids:
            if peer.peer_node_id in peer_ids:
                continue
            await peer.send_message(msg)

        if self.wallet_peers is not None:
            await self.wallet_peers.on_connect(peer)

    async def long_sync(
        self,
        target_height: uint32,
        full_node: WSChiaConnection,
        syncing: bool = False,
        fork_height: Optional[int] = None,
    ):
        """
        Sync algorithm:
        - Download and verify weight proof (if not trusted)
        - The minimum height is finished_sync_up_to - 32, roll back anything after it (if syncing)
        - Subscribe to all puzzle_hashes over and over until there are no more updates
        - Subscribe to all coin_ids over and over until there are no more updates
        - syncing=False means that we are just double-checking with this peer to make sure we don't have any
          missing transactions, so we don't need to rollback
        """

        trusted: bool = self.is_trusted(full_node)
        self.log.info(f"Starting sync trusted: {trusted} to peer {full_node.peer_host}")
        assert self.wallet_state_manager is not None
        start_time = time.time()
        # This is the greatest height where we have downloaded all transactions before it, and we know it's valid
        current_height: uint32 = await self.wallet_state_manager.blockchain.get_finished_sync_up_to()
        min_height: uint32 = uint32(max(0, current_height - 32))

        if syncing:
            await self.wallet_state_manager.reorg_rollback(min_height)
            self.rollback_request_caches(min_height)
            await self.update_ui()

        already_checked_ph: Set[bytes32] = set()
        continue_while: bool = True
        all_puzzle_hashes: List[bytes32] = await self.get_puzzle_hashes_to_subscribe()
        while continue_while:
            # Get all phs from puzzle store
            ph_chunks: List[List[bytes32]] = chunks(all_puzzle_hashes, 1000)
            for chunk in ph_chunks:
                ph_update_res: List[CoinState] = await self.subscribe_to_phs(
                    [p for p in chunk if p not in already_checked_ph], full_node, syncing, min_height, fork_height
                )
                await self.receive_state_from_peer(ph_update_res, full_node)
                already_checked_ph.update(chunk)

            # Check if new puzzle hashed have been created
            await self.wallet_state_manager.create_more_puzzle_hashes()
            all_puzzle_hashes = await self.get_puzzle_hashes_to_subscribe()
            continue_while = False
            for ph in all_puzzle_hashes:
                if ph not in already_checked_ph:
                    continue_while = True
                    break
        self.log.info(f"Successfully subscribed and updated {len(already_checked_ph)} puzzle hashes")

        continue_while = False
        all_coin_ids: List[bytes32] = await self.get_coin_ids_to_subscribe(min_height)
        already_checked_coin_ids: Set[bytes32] = set()
        while continue_while:
            one_k_chunks = chunks(all_coin_ids, 1000)
            for chunk in one_k_chunks:
                c_update_res: List[CoinState] = await self.subscribe_to_coin_updates(
                    chunk, full_node, syncing, min_height, fork_height
                )
                await self.receive_state_from_peer(c_update_res, full_node)
                already_checked_coin_ids.update(chunk)

            all_coin_ids = await self.get_coin_ids_to_subscribe(min_height)
            continue_while = False
            for coin_id in all_coin_ids:
                if coin_id not in already_checked_coin_ids:
                    continue_while = True
                    break
        self.log.info(f"Successfully subscribed and updated {len(already_checked_coin_ids)} coin ids")

        if target_height > await self.wallet_state_manager.blockchain.get_finished_sync_up_to():
            await self.wallet_state_manager.blockchain.set_finished_sync_up_to(target_height)

        if trusted:
            self.local_node_synced = True

        self.wallet_state_manager.state_changed("new_block")

        self.synced_peers.add(full_node.peer_node_id)
        await self.update_ui()

        end_time = time.time()
        duration = end_time - start_time
        self.log.info(f"Sync (trusted: {trusted}) duration was: {duration}")

    async def receive_state_from_peer(
        self,
        items: List[CoinState],
        peer: WSChiaConnection,
        fork_height: Optional[uint32] = None,
        height: Optional[uint32] = None,
        header_hash: Optional[bytes32] = None,
    ):
        # Adds the state to the wallet state manager. If the peer is trusted, we do not validate. If the peer is
        # untrusted we do, but we might not add the state, since we need to receive the new_peak message as well.

        assert self.wallet_state_manager is not None
        trusted = self.is_trusted(peer)
        # Validate states in parallel, apply serial
        if self.validation_semaphore is None:
            self.validation_semaphore = asyncio.Semaphore(6)
        if self.new_state_lock is None:
            self.new_state_lock = asyncio.Lock()

        # If there is a fork, we need to ensure that we roll back in trusted mode to properly handle reorgs
        if trusted and fork_height is not None and height is not None and fork_height != height - 1:
            await self.wallet_state_manager.reorg_rollback(fork_height)
        cache: PeerRequestCache = self.get_cache_for_peer(peer)
        if fork_height is not None:
            cache.clear_after_height(fork_height)

        all_tasks = []

        for idx, potential_state in enumerate(items):

            async def receive_and_validate(inner_state: CoinState, inner_idx: int):
                assert self.wallet_state_manager is not None
                assert self.validation_semaphore is not None
                # if height is not None:
                async with self.validation_semaphore:
                    try:
                        if header_hash is not None:
                            assert height is not None
                            self.add_state_to_race_cache(header_hash, height, inner_state)
                        if trusted:
                            valid = True
                        else:
                            valid = await self.validate_received_state_from_peer(inner_state, peer, cache, fork_height)
                        if valid:
                            self.log.info(f"new coin state received ({inner_idx + 1} / {len(items)})")
                            assert self.new_state_lock is not None
                            async with self.new_state_lock:
                                await self.wallet_state_manager.new_coin_state([inner_state], peer, fork_height)
                    except Exception as e:
                        tb = traceback.format_exc()
                        self.log.error(f"Exception while adding state: {e} {tb}")

            task = receive_and_validate(potential_state, idx)
            all_tasks.append(task)
            while len(self.validation_semaphore._waiters) > 20:
                self.log.debug("sleeping 2 sec")
                await asyncio.sleep(2)

        await asyncio.gather(*all_tasks)
        await self.update_ui()

    async def subscribe_to_phs(
        self,
        puzzle_hashes: List[bytes32],
        peer: WSChiaConnection,
        syncing: bool,
        min_height: uint32,
        fork_height: Optional[int],
    ) -> List[CoinState]:
        """
        Tells full nodes that we are interested in puzzle hashes, and returns the response.
        """
        assert self.wallet_state_manager is not None
        trusted: bool = self.is_trusted(peer)
        msg = wallet_protocol.RegisterForPhUpdates(puzzle_hashes, min_height)
        all_coins_state: Optional[RespondToPhUpdates] = await peer.register_interest_in_puzzle_hash(msg)

        final_coin_state: List[CoinState] = []
        if all_coins_state is not None:
            if not trusted and syncing is not None and not syncing:
                assert fork_height is not None
                final_coin_state = filter_coin_states(all_coins_state.coin_states, fork_height)
            else:
                final_coin_state = all_coins_state.coin_states
        return final_coin_state

    async def subscribe_to_coin_updates(
        self,
        coin_names: List[bytes32],
        peer: WSChiaConnection,
        syncing: Optional[bool],
        min_height: uint32,
        fork_height: Optional[int],
    ) -> List[CoinState]:
        """
        Tells full nodes that we are interested in coin ids, and returns the response.
        """
        trusted: bool = self.is_trusted(peer)

        msg = wallet_protocol.RegisterForCoinUpdates(coin_names, min_height)

        all_coins_state: Optional[RespondToCoinUpdates] = await peer.register_interest_in_coin(msg)

        final_coin_state: List[CoinState] = []
        if all_coins_state is not None:
            if not trusted and syncing is not None and not syncing:
                assert fork_height is not None
                final_coin_state = filter_coin_states(all_coins_state.coin_states, fork_height)
            else:
                final_coin_state = all_coins_state.coin_states
        return final_coin_state

    async def get_coins_with_puzzle_hash(self, puzzle_hash) -> List[CoinState]:
        assert self.wallet_state_manager is not None
        assert self.server is not None
        all_nodes = self.server.connection_by_type[NodeType.FULL_NODE]
        if len(all_nodes.keys()) == 0:
            raise ValueError("Not connected to the full node")
        first_node = list(all_nodes.values())[0]
        msg = wallet_protocol.RegisterForPhUpdates(puzzle_hash, uint32(0))
        coin_state: Optional[RespondToPhUpdates] = await first_node.register_interest_in_puzzle_hash(msg)
        assert coin_state is not None
        return coin_state.coin_states

    async def is_peer_synced(self, peer: WSChiaConnection, height: uint32, request_time: uint64) -> Optional[uint64]:
        request = wallet_protocol.RequestBlockHeader(height)
        header_response: Optional[RespondBlockHeader] = await peer.request_block_header(request)
        assert header_response is not None

        # Get last timestamp
        last_tx: Optional[HeaderBlock] = await fetch_last_tx_from_peer(height, peer)
        latest_timestamp: Optional[uint64] = None
        if last_tx is not None:
            assert last_tx.foliage_transaction_block is not None
            latest_timestamp = last_tx.foliage_transaction_block.timestamp

        # Return None if not synced
        if latest_timestamp is None or self.config["testing"] is False and latest_timestamp < request_time - 600:
            return None
        return latest_timestamp

    def is_trusted(self, peer) -> bool:
        assert self.server is not None
        return self.server.is_trusted_peer(peer, self.config["trusted_peers"])

    def add_state_to_race_cache(self, header_hash: bytes32, height: uint32, coin_state: CoinState) -> None:
        # Clears old state that is no longer relevant
        delete_threshold = 100
        for rc_height, rc_hh in self.race_cache_hashes:
            if height - delete_threshold >= rc_height:
                self.race_cache.pop(rc_hh)
        self.race_cache_hashes = [
            (rc_height, rc_hh) for rc_height, rc_hh in self.race_cache_hashes if height - delete_threshold < rc_height
        ]

        if header_hash not in self.race_cache:
            self.race_cache[header_hash] = set()
        self.race_cache[header_hash].add(coin_state)

    async def state_update_received(self, request: wallet_protocol.CoinStateUpdate, peer: WSChiaConnection) -> None:
        # This gets called from the full node every time there is a new coin or puzzle hash change in the DB
        # that is of interest to this wallet. It is not guaranteed to come for every height. This message is guaranteed
        # to come before the corresponding new_peak for each height. We handle this differently for trusted and
        # untrusted peers. For trusted, we always process the state, and we process reorgs as well.
        assert self.wallet_state_manager is not None
        assert self.server is not None

        # TODO: ensure we don't drop any things here, but allow waiting until sync is done.
        async with self._new_peak_lock_high_priority:
            async with self.wallet_state_manager.lock:
                await self.receive_state_from_peer(
                    request.items,
                    peer,
                    request.fork_height,
                    request.height,
                    request.peak_hash,
                )

    def get_full_node_peer(self) -> Optional[WSChiaConnection]:
        nodes = self.server.get_full_node_connections()
        if len(nodes) > 0:
            return nodes[0]
        else:
            return None

    async def disconnect_and_stop_wpeers(self):
        # Close connection of non trusted peers
        if len(self.server.get_full_node_connections()) > 1:
            for peer in self.server.get_full_node_connections():
                if not self.is_trusted(peer):
                    await peer.close()

        if self.wallet_peers is not None:
            await self.wallet_peers.ensure_is_closed()
            self.wallet_peers = None

    async def get_timestamp_for_height(self, height: uint32) -> uint64:
        """
        Returns the timestamp for transaction block at h=height, if not transaction block, backtracks until it finds
        a transaction block
        """
        if height in self.height_to_time:
            return self.height_to_time[height]

        for cache in self.untrusted_caches.values():
            if height in cache.blocks:
                block = cache.blocks[height]
                if (
                    block.foliage_transaction_block is not None
                    and block.foliage_transaction_block.timestamp is not None
                ):
                    self.height_to_time[height] = block.foliage_transaction_block.timestamp
                    return block.foliage_transaction_block.timestamp
        peer: Optional[WSChiaConnection] = self.get_full_node_peer()
        if peer is None:
            raise ValueError("Cannot fetch timestamp, no peers")
        last_tx_block: Optional[HeaderBlock] = await fetch_last_tx_from_peer(height, peer)
        if last_tx_block is None:
            raise ValueError(f"Error fetching blocks from peer {peer.get_peer_info()}")
        return last_tx_block.foliage_transaction_block.timestamp

    async def new_peak_wallet(self, peak: wallet_protocol.NewPeakWallet, peer: WSChiaConnection):
        self.log.debug(f"New peak wallet.. {peak.height} {peer.get_peer_info()}")
        assert self.wallet_state_manager is not None
        assert self.server is not None
        self._node_peaks[peer.peer_node_id] = (peak.height, peak.header_hash)
        request_time = uint64(int(time.time()))
        trusted: bool = self.is_trusted(peer)
        peak_hb: Optional[HeaderBlock] = await self.wallet_state_manager.blockchain.get_peak_block()
        if peak_hb is not None and peak.weight < peak_hb.weight:
            # Discards old blocks, but accepts blocks that are equal in weight to peak
            return

        # This lock will prevent two new_peaks from being processed at the same time. It will also prevent state
        # updates from being processed. State updates must be processed before the new_peak messages for each height.
        # If there was no coin_state_update before the new_peak message, we will assume that there were no tx of
        # interest for the wallet.
        # TODO: handle the stackig of too many new_peaks in the lock during a long sync
        async with self._new_peak_lock_low_priority:
            self.log.debug(f"Processing new peak {peak.height} {peak.header_hash}")
            peak_hb = await self.wallet_state_manager.blockchain.get_peak_block()
            if peak_hb is not None and peak.weight < peak_hb.weight:
                return

            if self.wallet_state_manager is None:
                # When logging out of wallet
                return
            latest_timestamp: Optional[uint64] = await self.is_peer_synced(peer, peak.height, request_time)
            if latest_timestamp is None:
                if trusted:
                    self.log.debug(f"Trusted peer {peer.get_peer_info()} is not synced.")
                    return
                else:
                    self.log.warning(f"Non-trusted peer {peer.get_peer_info()} is not synced, disconnecting")
                    await peer.close(120)
                    return
            request = wallet_protocol.RequestBlockHeader(peak.height)
            response: Optional[RespondBlockHeader] = await peer.request_block_header(request)
            if response is None:
                self.log.warning(f"Peer {peer.get_peer_info()} did not respond in time.")
                await peer.close(120)
                return
            header_block: HeaderBlock = response.header_block

            if self.is_trusted(peer):
                async with self.wallet_state_manager.lock:
                    await self.wallet_state_manager.blockchain.set_peak_block(header_block, latest_timestamp)
                    # Disconnect from all untrusted peers if our local node is trusted and synced
                    await self.disconnect_and_stop_wpeers()

                    # Sync to trusted node if we haven't done so yet. As long as we have synced once (and not
                    # disconnected), we assume that the full node will continue to give us state updates, so we do
                    # not need to resync.
                    if peer.peer_node_id not in self.synced_peers:
                        await self.long_sync(peak.height, peer, True, None)
                        self.wallet_state_manager.set_sync_mode(False)
            else:
                far_behind: bool = (
                    peak.height - self.wallet_state_manager.blockchain.get_peak_height() > self.LONG_SYNC_THRESHOLD
                )

                # check if claimed peak is heavier or same as our current peak
                # if we haven't synced fully to this peer sync again
                if (
                    peer.peer_node_id not in self.synced_peers or far_behind
                ) and peak.height >= self.constants.WEIGHT_PROOF_RECENT_BLOCKS:
                    syncing = False
                    if far_behind or len(self.synced_peers) == 0:
                        syncing = True
                        self.wallet_state_manager.set_sync_mode(True)
                    try:
                        (
                            valid_weight_proof,
                            weight_proof,
                            summaries,
                            block_records,
                        ) = await self.fetch_and_validate_the_weight_proof(peer, response.header_block)
                        if valid_weight_proof is False:
                            if syncing:
                                self.wallet_state_manager.set_sync_mode(False)
                            await peer.close()
                            return
                        assert weight_proof is not None
                        old_proof = self.wallet_state_manager.blockchain.synced_weight_proof
                        fork_point = 0
                        if old_proof is not None:
                            fork_point = self.wallet_state_manager.weight_proof_handler.get_fork_point(
                                old_proof, weight_proof
                            )

                        await self.wallet_state_manager.blockchain.new_weight_proof(weight_proof, block_records)
                        if syncing:
                            async with self.wallet_state_manager.lock:
                                await self.long_sync(peak.height, peer, syncing, fork_point)
                        else:
                            await self.long_sync(peak.height, peer, syncing, fork_point)
                        if (
                            self.wallet_state_manager.blockchain.synced_weight_proof is None
                            or weight_proof.recent_chain_data[-1].weight
                            > self.wallet_state_manager.blockchain.synced_weight_proof.recent_chain_data[-1].weight
                        ):
                            await self.wallet_state_manager.blockchain.new_weight_proof(weight_proof, block_records)
                    except Exception:
                        if syncing:
                            self.wallet_state_manager.set_sync_mode(False)
                        tb = traceback.format_exc()
                        self.log.error(f"Error syncing to {peer.get_peer_info()} {tb}")
                        await peer.close()
                        return
                    if syncing:
                        self.wallet_state_manager.set_sync_mode(False)

                else:
                    # This is the (untrusted) case where we already synced and are not too far behind. Here we just
                    # fetch one by one.
                    async with self.wallet_state_manager.lock:
                        backtrack_fork_height = await self.wallet_short_sync_backtrack(header_block, peer)

                        if peer.peer_node_id not in self.synced_peers:
                            # Edge case, this happens when the peak < WEIGHT_PROOF_RECENT_BLOCKS
                            # we still want to subscribe for all phs and coins.
                            # (Hints are not in filter)
                            all_coin_ids: List[bytes32] = await self.get_coin_ids_to_subscribe(uint32(0))
                            phs: List[bytes32] = await self.get_puzzle_hashes_to_subscribe()
                            ph_updates: List[CoinState] = await self.subscribe_to_phs(phs, peer, True, uint32(0), None)
                            coin_updates: List[CoinState] = await self.subscribe_to_coin_updates(
                                all_coin_ids, peer, True, uint32(0), None
                            )
                            peer_new_peak_height, peer_new_peak_hash = self._node_peaks[peer.peer_node_id]
                            await self.receive_state_from_peer(
                                ph_updates + coin_updates,
                                peer,
                                height=peer_new_peak_height,
                                header_hash=peer_new_peak_hash,
                            )
                            self.synced_peers.add(peer.peer_node_id)

                        # For every block, we need to apply the cache from race_cache
                        for potential_height in range(backtrack_fork_height + 1, peak.height + 1):
                            header_hash = self.wallet_state_manager.blockchain.height_to_hash(uint32(potential_height))
                            if header_hash in self.race_cache:
                                self.log.debug(f"Receiving race state: {self.race_cache[header_hash]}")
                                await self.receive_state_from_peer(list(self.race_cache[header_hash]), peer)

                        self.wallet_state_manager.set_sync_mode(False)
                        self.wallet_state_manager.state_changed("new_block")
                        self.log.info(f"Finished processing new peak of {peak.height}")

            if (
                peer.peer_node_id in self.synced_peers
                and peak.height > await self.wallet_state_manager.blockchain.get_finished_sync_up_to()
            ):
                await self.wallet_state_manager.blockchain.set_finished_sync_up_to(peak.height)
            await self.wallet_state_manager.new_peak(peak)

    async def wallet_short_sync_backtrack(self, header_block: HeaderBlock, peer: WSChiaConnection) -> int:
        assert self.wallet_state_manager is not None
        peak: Optional[HeaderBlock] = await self.wallet_state_manager.blockchain.get_peak_block()

        top = header_block
        blocks = [top]
        # Fetch blocks backwards until we hit the one that we have,
        # then complete them with additions / removals going forward
        fork_height = 0
        if self.wallet_state_manager.blockchain.contains_block(header_block.prev_header_hash):
            fork_height = header_block.height - 1

        while not self.wallet_state_manager.blockchain.contains_block(top.prev_header_hash) and top.height > 0:
            request_prev = wallet_protocol.RequestBlockHeader(top.height - 1)
            response_prev: Optional[RespondBlockHeader] = await peer.request_block_header(request_prev)
            if response_prev is None or not isinstance(response_prev, RespondBlockHeader):
                raise RuntimeError("bad block header response from peer while syncing")
            prev_head = response_prev.header_block
            blocks.append(prev_head)
            top = prev_head
            fork_height = top.height - 1

        blocks.reverse()
        # Roll back coins and transactions
        peak_height = self.wallet_state_manager.blockchain.get_peak_height()
        if fork_height < peak_height:
            self.log.info(f"Rolling back to {fork_height}")
            await self.wallet_state_manager.reorg_rollback(fork_height)
            await self.update_ui()
        self.rollback_request_caches(fork_height)

        if peak is not None:
            assert header_block.weight >= peak.weight
        for block in blocks:
            # Set blockchain to the latest peak
            res, err = await self.wallet_state_manager.blockchain.receive_block(block)
            if res == ReceiveBlockResult.INVALID_BLOCK:
                raise ValueError(err)

        return fork_height

    async def update_ui(self):
        for wallet_id, wallet in self.wallet_state_manager.wallets.items():
            self.wallet_state_manager.state_changed("coin_removed", wallet_id)
            self.wallet_state_manager.state_changed("coin_added", wallet_id)

    async def fetch_and_validate_the_weight_proof(
        self, peer: WSChiaConnection, peak: HeaderBlock
    ) -> Tuple[bool, Optional[WeightProof], List[SubEpochSummary], List[BlockRecord]]:
        assert self.wallet_state_manager is not None
        assert self.wallet_state_manager.weight_proof_handler is not None

        weight_request = RequestProofOfWeight(peak.height, peak.header_hash)
        weight_proof_response: RespondProofOfWeight = await peer.request_proof_of_weight(weight_request, timeout=60)

        if weight_proof_response is None:
            return False, None, [], []
        start_validation = time.time()

        weight_proof = weight_proof_response.wp

        if weight_proof.recent_chain_data[-1].reward_chain_block.height != peak.height:
            return False, None, [], []
        if weight_proof.recent_chain_data[-1].reward_chain_block.weight != peak.weight:
            return False, None, [], []

        if weight_proof.get_hash() in self.valid_wp_cache:
            valid, fork_point, summaries, block_records = self.valid_wp_cache[weight_proof.get_hash()]
        else:
            start_validation = time.time()
            (
                valid,
                fork_point,
                summaries,
                block_records,
            ) = await self.wallet_state_manager.weight_proof_handler.validate_weight_proof(weight_proof)
            if valid:
                self.valid_wp_cache[weight_proof.get_hash()] = valid, fork_point, summaries, block_records

        end_validation = time.time()
        self.log.info(f"It took {end_validation - start_validation} time to validate the weight proof")
        return valid, weight_proof, summaries, block_records

    async def get_puzzle_hashes_to_subscribe(self) -> List[bytes32]:
        assert self.wallet_state_manager is not None
        all_puzzle_hashes = list(await self.wallet_state_manager.puzzle_store.get_all_puzzle_hashes())
        # Get all phs from interested store
        interested_puzzle_hashes = [
            t[0] for t in await self.wallet_state_manager.interested_store.get_interested_puzzle_hashes()
        ]
        all_puzzle_hashes.extend(interested_puzzle_hashes)
        return all_puzzle_hashes

    async def get_coin_ids_to_subscribe(self, min_height: uint32) -> List[bytes32]:
        assert self.wallet_state_manager is not None
        all_coins: Set[WalletCoinRecord] = await self.wallet_state_manager.coin_store.get_coins_to_check(min_height)
        all_coin_names: Set[bytes32] = {coin_record.name() for coin_record in all_coins}
        removed_dict = await self.wallet_state_manager.trade_manager.get_coins_of_interest()
        all_coin_names.update(removed_dict.keys())
        all_coin_names.update(await self.wallet_state_manager.interested_store.get_interested_coin_ids())
        return list(all_coin_names)

    async def validate_received_state_from_peer(
        self,
        coin_state: CoinState,
        peer: WSChiaConnection,
        peer_request_cache: PeerRequestCache,
        fork_height: Optional[uint32],
    ) -> bool:
        """
        Returns all state that is valid and included in the blockchain proved by the weight proof. If return_old_states
        is False, only new states that are not in the coin_store are returned.
        """
        assert self.wallet_state_manager is not None

        # Only use the cache if we are talking about states before the fork point. If we are evaluating something
        # in a reorg, we cannot use the cache, since we don't know if it's actually in the new chain after the reorg.
        if await can_use_peer_request_cache(coin_state, peer_request_cache, fork_height):
            return True

        spent_height = coin_state.spent_height
        confirmed_height = coin_state.created_height
        current = await self.wallet_state_manager.coin_store.get_coin_record(coin_state.coin.name())
        # if remote state is same as current local state we skip validation

        # CoinRecord unspent = height 0, coin state = None. We adjust for comparison below
        current_spent_height = None
        if current is not None and current.spent_block_height != 0:
            current_spent_height = current.spent_block_height

        # Same as current state, nothing to do
        if (
            current is not None
            and current_spent_height == spent_height
            and current.confirmed_block_height == confirmed_height
        ):
            return True

        reorg_mode = False
        if current is not None and confirmed_height is None:
            # This coin got reorged
            reorg_mode = True
            confirmed_height = current.confirmed_block_height

        if confirmed_height is None:
            return False

        # request header block for created height
        if confirmed_height in peer_request_cache.blocks and reorg_mode is False:
            state_block: HeaderBlock = peer_request_cache.blocks[confirmed_height]
        else:
            request = RequestHeaderBlocks(confirmed_height, confirmed_height)
            res = await peer.request_header_blocks(request)
            state_block = res.header_blocks[0]
            peer_request_cache.blocks[confirmed_height] = state_block

        # get proof of inclusion
        assert state_block.foliage_transaction_block is not None
        validate_additions_result = await request_and_validate_additions(
            peer,
            state_block.height,
            state_block.header_hash,
            coin_state.coin.puzzle_hash,
            state_block.foliage_transaction_block.additions_root,
        )

        if validate_additions_result is False:
            self.log.warning("Validate false 1")
            await peer.close(9999)
            return False

        # get blocks on top of this block
        validated = await self.validate_block_inclusion(state_block, peer, peer_request_cache)
        if not validated:
            return False

        if spent_height is None and current is not None and current.spent_block_height != 0:
            # Peer is telling us that coin that was previously known to be spent is not spent anymore
            # Check old state
            if current.spent_block_height != spent_height:
                reorg_mode = True
            if spent_height in peer_request_cache.blocks and reorg_mode is False:
                spent_state_block: HeaderBlock = peer_request_cache.blocks[current.spent_block_height]
            else:
                request = RequestHeaderBlocks(current.spent_block_height, current.spent_block_height)
                res = await peer.request_header_blocks(request)
                spent_state_block = res.header_blocks[0]
                assert spent_state_block.height == current.spent_block_height
                peer_request_cache.blocks[current.spent_block_height] = spent_state_block
            assert spent_state_block.foliage_transaction_block is not None
            validate_removals_result: bool = await request_and_validate_removals(
                peer,
                current.spent_block_height,
                spent_state_block.header_hash,
                coin_state.coin.name(),
                spent_state_block.foliage_transaction_block.removals_root,
            )
            if validate_removals_result is False:
                self.log.warning("Validate false 2")
                await peer.close(9999)
                return False
            validated = await self.validate_block_inclusion(spent_state_block, peer, peer_request_cache)
            if not validated:
                return False

        if spent_height is not None:
            # request header block for created height
            if spent_height in peer_request_cache.blocks:
                spent_state_block = peer_request_cache.blocks[spent_height]
            else:
                request = RequestHeaderBlocks(spent_height, spent_height)
                res = await peer.request_header_blocks(request)
                spent_state_block = res.header_blocks[0]
                assert spent_state_block.height == spent_height
                peer_request_cache.blocks[spent_height] = spent_state_block
            assert spent_state_block.foliage_transaction_block is not None
            validate_removals_result = await request_and_validate_removals(
                peer,
                spent_state_block.height,
                spent_state_block.header_hash,
                coin_state.coin.name(),
                spent_state_block.foliage_transaction_block.removals_root,
            )
            if validate_removals_result is False:
                self.log.warning("Validate false 3")
                await peer.close(9999)
                return False
            validated = await self.validate_block_inclusion(spent_state_block, peer, peer_request_cache)
            if not validated:
                return False
        peer_request_cache.states_validated[coin_state.get_hash()] = coin_state
        return True

    async def validate_block_inclusion(
        self, block: HeaderBlock, peer: WSChiaConnection, peer_request_cache: PeerRequestCache
    ) -> bool:
        assert self.wallet_state_manager is not None
        if self.wallet_state_manager.blockchain.contains_height(block.height):
            stored_hash = self.wallet_state_manager.blockchain.height_to_hash(block.height)
            stored_record = self.wallet_state_manager.blockchain.try_block_record(stored_hash)
            if stored_record is not None:
                if stored_record.header_hash == block.header_hash:
                    return True

        weight_proof = self.wallet_state_manager.blockchain.synced_weight_proof
        if weight_proof is None:
            return False

        if block.height >= weight_proof.recent_chain_data[0].height:
            # this was already validated as part of the wp validation
            index = block.height - weight_proof.recent_chain_data[0].height
            if weight_proof.recent_chain_data[index].header_hash != block.header_hash:
                self.log.error("Failed validation 1")
                return False
            return True
        else:
            start = block.height + 1
            compare_to_recent = False
            current_ses: Optional[SubEpochData] = None
            inserted: Optional[SubEpochData] = None
            first_height_recent = weight_proof.recent_chain_data[0].height
            if start > first_height_recent - 1000:
                compare_to_recent = True
                end = first_height_recent
            else:
                request = RequestSESInfo(block.height, block.height + 32)
                if block.height in peer_request_cache.ses_requests:
                    res_ses: RespondSESInfo = peer_request_cache.ses_requests[block.height]
                else:
                    res_ses = await peer.request_ses_hashes(request)
                    peer_request_cache.ses_requests[block.height] = res_ses

                ses_0 = res_ses.reward_chain_hash[0]
                last_height = res_ses.heights[0][-1]  # Last height in sub epoch
                end = last_height
                for idx, ses in enumerate(weight_proof.sub_epochs):
                    if idx > len(weight_proof.sub_epochs) - 3:
                        break
                    if ses.reward_chain_hash == ses_0:
                        current_ses = ses
                        inserted = weight_proof.sub_epochs[idx + 2]
                        break
                if current_ses is None:
                    self.log.error("Failed validation 2")
                    return False

            blocks: List[HeaderBlock] = []
            for i in range(start - (start % 32), end + 1, 32):
                request_start = min(uint32(i), end)
                request_end = min(uint32(i + 31), end)
                request_h_response = RequestHeaderBlocks(request_start, request_end)
                if (request_start, request_end) in peer_request_cache.block_requests:
                    self.log.info(f"Using cache for blocks {request_start} - {request_end}")
                    res_h_blocks: Optional[RespondHeaderBlocks] = peer_request_cache.block_requests[
                        (request_start, request_end)
                    ]
                else:
                    start_time = time.time()
                    res_h_blocks = await peer.request_header_blocks(request_h_response)
                    if res_h_blocks is None:
                        self.log.error("Failed validation 2.5")
                        return False
                    end_time = time.time()
                    peer_request_cache.block_requests[(request_start, request_end)] = res_h_blocks
                    self.log.info(
                        f"Fetched blocks: {request_start} - {request_end} | duration: {end_time - start_time}"
                    )
                assert res_h_blocks is not None
                blocks.extend([bl for bl in res_h_blocks.header_blocks if bl.height >= start])

            if compare_to_recent and weight_proof.recent_chain_data[0].header_hash != blocks[-1].header_hash:
                self.log.error("Failed validation 3")
                return False

            reversed_blocks = blocks.copy()
            reversed_blocks.reverse()

            if not compare_to_recent:
                last = reversed_blocks[0].finished_sub_slots[-1].reward_chain.get_hash()
                if inserted is None or last != inserted.reward_chain_hash:
                    self.log.error("Failed validation 4")
                    return False

            for idx, en_block in enumerate(reversed_blocks):
                if idx == len(reversed_blocks) - 1:
                    next_block_rc_hash = block.reward_chain_block.get_hash()
                    prev_hash = block.header_hash
                else:
                    next_block_rc_hash = reversed_blocks[idx + 1].reward_chain_block.get_hash()
                    prev_hash = reversed_blocks[idx + 1].header_hash

                if not en_block.prev_header_hash == prev_hash:
                    self.log.error("Failed validation 5")
                    return False

                if len(en_block.finished_sub_slots) > 0:
                    #  What to do here
                    reversed_slots = en_block.finished_sub_slots.copy()
                    reversed_slots.reverse()
                    for slot_idx, slot in enumerate(reversed_slots[:-1]):
                        hash_val = reversed_slots[slot_idx + 1].reward_chain.get_hash()
                        if not hash_val == slot.reward_chain.end_of_slot_vdf.challenge:
                            self.log.error("Failed validation 6")
                            return False
                    if not next_block_rc_hash == reversed_slots[-1].reward_chain.end_of_slot_vdf.challenge:
                        self.log.error("Failed validation 7")
                        return False
                else:
                    if not next_block_rc_hash == en_block.reward_chain_block.reward_chain_ip_vdf.challenge:
                        self.log.error("Failed validation 8")
                        return False

                if idx > len(reversed_blocks) - 50:
                    if not AugSchemeMPL.verify(
                        en_block.reward_chain_block.proof_of_space.plot_public_key,
                        en_block.foliage.foliage_block_data.get_hash(),
                        en_block.foliage.foliage_block_data_signature,
                    ):
                        self.log.error("Failed validation 9")
                        return False
            return True

    async def fetch_puzzle_solution(self, peer, height: uint32, coin: Coin) -> CoinSpend:
        solution_response = await peer.request_puzzle_solution(
            wallet_protocol.RequestPuzzleSolution(coin.name(), height)
        )
        if solution_response is None or not isinstance(solution_response, wallet_protocol.RespondPuzzleSolution):
            raise ValueError(f"Was not able to obtain solution {solution_response}")
        assert solution_response.response.puzzle.get_tree_hash() == coin.puzzle_hash
        assert solution_response.response.coin_name == coin.name()

        return CoinSpend(
            coin,
            solution_response.response.puzzle.to_serialized_program(),
            solution_response.response.solution.to_serialized_program(),
        )

    async def get_coin_state(self, coin_names: List[bytes32], fork_height: Optional[uint32] = None) -> List[CoinState]:
        assert self.server is not None
        all_nodes = self.server.connection_by_type[NodeType.FULL_NODE]
        if len(all_nodes.keys()) == 0:
            raise ValueError("Not connected to the full node")
        first_node = list(all_nodes.values())[0]
        msg = wallet_protocol.RegisterForCoinUpdates(coin_names, uint32(0))
        coin_state: Optional[RespondToCoinUpdates] = await first_node.register_interest_in_coin(msg)
        assert coin_state is not None

        if not self.is_trusted(first_node):
            valid_list = []
            for coin in coin_state.coin_states:
                valid = await self.validate_received_state_from_peer(
                    coin, first_node, self.get_cache_for_peer(first_node), fork_height
                )
                if valid:
                    valid_list.append(coin)
            return valid_list

        return coin_state.coin_states

    async def fetch_children(
        self, peer: WSChiaConnection, coin_name: bytes32, fork_height: Optional[uint32] = None
    ) -> List[CoinState]:
        response: Optional[wallet_protocol.RespondChildren] = await peer.request_children(
            wallet_protocol.RequestChildren(coin_name)
        )
        if response is None or not isinstance(response, wallet_protocol.RespondChildren):
            raise ValueError(f"Was not able to obtain children {response}")

        if not self.is_trusted(peer):
            request_cache = self.get_cache_for_peer(peer)
            validated = []
            for state in response.coin_states:
                valid = await self.validate_received_state_from_peer(state, peer, request_cache, fork_height)
                if valid:
                    validated.append(state)
            return validated
        return response.coin_states

    # For RPC only. You should use wallet_state_manager.add_pending_transaction for normal wallet business.
    async def push_tx(self, spend_bundle):
        msg = make_msg(
            ProtocolMessageTypes.send_transaction,
            wallet_protocol.SendTransaction(spend_bundle),
        )
        full_nodes = self.server.get_full_node_connections()
        for peer in full_nodes:
            await peer.send_message(msg)
