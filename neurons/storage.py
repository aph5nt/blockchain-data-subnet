import concurrent
import traceback
import time
from typing import Optional

import bittensor as bt
import websocket
from pydantic import BaseModel

from insights.protocol import get_network_id, get_model_id
from neurons import VERSION
from neurons.docker_utils import get_docker_image_version

class Metadata(BaseModel):
    def to_compact(self):
        return ','.join(f"{key}:{repr(getattr(self, key))}" for key in self.__dict__)

class MinerMetadata(Metadata):
    b: int
    v: int
    di: str
    n: int
    mt: int
    ri: str

    @staticmethod
    def from_compact(compact_str):
        data_dict = {}
        for item in compact_str.split(','):
            key, value = item.split(':', 1)
            data_dict[key] = value.strip("'")
        return MinerMetadata(**data_dict)

class ValidatorMetadata(Metadata):
    b: int
    v: int
    di: str

    @staticmethod
    def from_compact(compact_str):
        data_dict = {}
        for item in compact_str.split(','):
            key, value = item.split(':', 1)
            data_dict[key] = value.strip("'")
        return ValidatorMetadata(**data_dict)

def store_miner_metadata(config, graph_search, wallet):
    def get_metadata():
        run_id = graph_search.get_run_id()
        docker_image = get_docker_image_version()
        return MinerMetadata(
            b=subtensor.block,
            n=get_network_id(config.network),
            mt=get_model_id(config.model_type),
            v=VERSION,
            di=docker_image,
            ri=run_id,
        )

    subtensor = bt.subtensor(config=config)

    try:
        metadata = get_metadata()
        subtensor.commit(wallet, config.netuid, Metadata.to_compact(metadata))
        bt.logging.info(f"Stored miner metadata: {metadata}")
    except bt.errors.MetadataError as e:
        bt.logging.warning(f"Skipping storing miner metadata")

def get_miners_metadata(config, metagraph):
    miners_metadata = {}

    bt.logging.info(f"Getting miner metadata for {len(metagraph.axons)} axons")

    def process_miner(axon):
        hotkey = axon.hotkey
        subtensor = bt.subtensor(config=config)
        while True:
            try:
                uid = subtensor.get_uid_for_hotkey_on_subnet(hotkey, config.netuid)
                metadata_str = subtensor.get_commitment(config.netuid, uid)
                if metadata_str is not None:
                    return hotkey, MinerMetadata.from_compact(metadata_str)
            except websocket._exceptions.WebSocketConnectionClosedException as e:
                bt.logging.debug(f"WebSocket Error for {hotkey}: {e}. Retrying...")
                time.sleep(bt.__blocktime__)
            except TimeoutError as e:
                bt.logging.debug(f"Timeout Error for {hotkey}: {e}. Retrying...")
                time.sleep(bt.__blocktime__)
            except Exception as e:
                if "int() argument must be a string" in str(e.args):
                    bt.logging.debug(f"Error while getting miner metadata {e}  Retrying...")
                    time.sleep(bt.__blocktime__)
                else:
                    bt.logging.warning(f"Error while getting miner metadata for {hotkey}, Skipping...")
                    return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_to_axon = {executor.submit(process_miner, axon): axon for axon in metagraph.axons if axon.is_serving}
        for future in concurrent.futures.as_completed(future_to_axon):
            result = future.result()
            if result:
                hotkey, metadata = result
                miners_metadata[hotkey] = metadata

    bt.logging.info(f"Got miner metadata for {len(miners_metadata)}/{len(metagraph.axons)}: {miners_metadata}")

    return miners_metadata

def store_validator_metadata(config, wallet):

    subtensor = bt.subtensor(config=config)

    try:
        docker_image = get_docker_image_version()
        metadata =  ValidatorMetadata(
            b=subtensor.block,
            v=VERSION,
            di=docker_image,
        )
        subtensor.commit(wallet, config.netuid, metadata.to_compact())
        bt.logging.info(f"Stored validator metadata: {metadata}")
    except bt.errors.MetadataError as e:
        bt.logging.warning(f"Skipping storing validator metadata")

def get_validator_metadata(config, metagraph):
    validator_metadata = {}

    def process_neuron(neuron):
        hotkey = neuron.hotkey
        subtensor = bt.subtensor(config=config)
        while True:
            try:
                uid = subtensor.get_uid_for_hotkey_on_subnet(hotkey, config.netuid)
                metadata_str = subtensor.get_commitment(config.netuid, uid)
                if metadata_str is not None:
                    metadata = ValidatorMetadata.from_compact(metadata_str)
                    bt.logging.info(f"Updated validator metadata for: {metadata}")
                    return hotkey, metadata
            except websocket._exceptions.WebSocketConnectionClosedException as e:
                bt.logging.debug(f"WebSocket Error for {hotkey}: {e}. Retrying...")
                time.sleep(bt.__blocktime__)
            except TimeoutError as e:
                bt.logging.debug(f"Timeout Error for {hotkey}: {e}. Retrying...")
                time.sleep(bt.__blocktime__)
            except Exception as e:
                if "int() argument must be a string" in str(e.args):
                    bt.logging.debug(f"Error while getting validator metadata {e}  Retrying...")
                    time.sleep(bt.__blocktime__)
                else:
                    bt.logging.warning(f"Error while getting validator metadata for {hotkey}, Skipping...")
                    return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_to_neuron = {
            executor.submit(process_neuron, neuron): neuron for neuron in metagraph.neurons
            if neuron.axon_info.ip == '0.0.0.0'
        }
        for future in concurrent.futures.as_completed(future_to_neuron):
            result = future.result()
            if result:
                hotkey, metadata = result
                if result:
                    validator_metadata[hotkey] = metadata

    bt.logging.info(f"Got validator metadata: {validator_metadata}")
    return validator_metadata