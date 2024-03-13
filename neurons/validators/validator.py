# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 aph5nt


# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import time
import argparse
import random
import torch
import bittensor as bt
import os
import yaml
import numpy as np

from insights import protocol
from insights.protocol import DiscoveryOutput, BlockCheckOutput, MAX_MULTIPLE_IPS, \
    MAX_MULTIPLE_RUN_ID

from neurons.remote_config import ValidatorConfig
from neurons.nodes.factory import NodeFactory
from neurons.storage import store_validator_metadata, get_miners_metadata
from neurons.validators.scoring import Scorer

from neurons.validators.utils.utils import get_miner_distributions, count_hotkeys_per_ip, count_run_id_per_hotkey
from neurons.validators.utils.uids import get_random_uids

from template.base.validator import BaseValidatorNeuron
class Validator(BaseValidatorNeuron):

    @staticmethod
    def get_config():

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--alpha", default=0.9, type=float, help="The weight moving average scoring.py."
        )

        parser.add_argument("--netuid", type=int, default=15, help="The chain subnet uid.")
        parser.add_argument("--dev", action=argparse.BooleanOptionalAction)

        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)

        config = bt.config(parser)
        
        dev = config.dev
        if dev:
            dev_config_path = "validator.yml"
            if os.path.exists(dev_config_path):
                with open(dev_config_path, 'r') as f:
                    dev_config = yaml.safe_load(f.read())
                config.update(dev_config)
                bt.logging.info(f"config updated with {dev_config_path}")

            else:
                with open(dev_config_path, 'w') as f:
                    yaml.safe_dump(config, f)
                bt.logging.info(f"config stored in {dev_config_path}")

        return config

    def __init__(self, config=None):
        config=Validator.get_config()
        self.validator_config = ValidatorConfig().load_and_get_config_values()
        networks = self.validator_config.get_networks()
        self.nodes = {network : NodeFactory.create_node(network) for network in networks}
        self.block_height_cache = {network: self.nodes[network].get_current_block_height() for network in networks}
        
        super(Validator, self).__init__(config)

        self.sync_validator()

    @staticmethod
    def validate_blockchain_range(start_block_height, last_block_height, min_range_size, current_block_height):
        if start_block_height is None or not isinstance(start_block_height, int):
            bt.logging.debug("Invalid start block height provided to cross_validate")
            return False
        if last_block_height is None or not isinstance(last_block_height, int):
            bt.logging.debug("Invalid last block height provided to cross_validate")
            return False
        if start_block_height <= 0 or last_block_height <= 0:
            bt.logging.debug("Negative block heights provided to cross_validate")
            return False
        if start_block_height >= last_block_height:
            bt.logging.debug("Start block height is greater than or equal to last block height in cross_validate")
            return False
        if not min_range_size:
            bt.logging.debug("Parameter 'min_range_size' is not truthy in cross_validate")
            return False
        if last_block_height > current_block_height + 3:
            bt.logging.debug("Last block height provided is larger than current block height")
            return False
        if (last_block_height + 1 - start_block_height) < min_range_size:
            bt.logging.debug("Miner block height is Invalid")
            return False

        return True

    def cross_validate(self, axon, node, start_block_height, last_block_height, min_range_size=20):
        current_block_height = node.get_current_block_height()
        if current_block_height is None:
            raise Exception("Failed to get current block height")

        if not self.validate_blockchain_range(start_block_height, last_block_height, min_range_size, current_block_height):
            return False, 0

        blocks_to_check = random.sample(range(start_block_height, last_block_height + 1), k=min_range_size)
        response = self.dendrite.query(
            axon,
            protocol.BlockCheck(blocks_to_check=blocks_to_check),
            deserialize=True,
            timeout = self.validator_config.discovery_timeout,
        )
        if response.output is None or len(response.output.data_samples)==0 or response.output.data_samples[0] is None:
            bt.logging.debug(f"Skipping response {response}")
            return None, None

        result = node.validate_all_data_samples(response.output.data_samples, blocks_to_check)
        response_time = response.dendrite.process_time
        return result, response_time


    def get_reward(self, response: DiscoveryOutput, ip_per_hotkey=None, run_id_per_hotkey=None, miner_distribution=None):
        output: DiscoveryOutput = response.output
        network = output.metadata.network
        start_block_height = output.start_block_height
        last_block_height = output.block_height
        axon_ip = response.axon.ip
        hot_key = response.axon.hotkey
        bt.logging.info(f"🔄 Processing response for {hot_key}@{axon_ip}")

        multiple_ips = ip_per_hotkey[axon_ip] > MAX_MULTIPLE_IPS
        multiple_run_ids = run_id_per_hotkey[hot_key] > MAX_MULTIPLE_RUN_ID

        try:
            cross_validation_result, response_time = self.cross_validate(response.axon, self.nodes[network], start_block_height, last_block_height)
        except Exception as e:
            bt.logging.error(f"Cross-Validation: {hot_key=} Exception {e=} skipping response")
            return None

        if cross_validation_result is None:
            bt.logging.debug(f"Cross-Validation: {hot_key=} Timeout skipping response")
            return None
        if not cross_validation_result:
            bt.logging.info(f"Cross-Validation: {hot_key=} Test failed")
            return 0
        bt.logging.info(f"Cross-Validation: {hot_key=} Test passed")

        if self.validator_config.is_grace_period and output.version != 5:
            score = self.validator_config.grace_threshold_score
            bt.logging.info(f"Miner version: {output.version}, setting score to: {score}")
        else:
            score = self.scorer.calculate_score(
                network,
                response_time,
                start_block_height,
                last_block_height,
                self.block_height_cache[network],
                miner_distribution,
                multiple_ips,
                multiple_run_ids
            )

        return score

    async def forward(self):
        # Add remote config
        self.validator_config = ValidatorConfig().load_and_get_config_values()
        ##
        available_uids = get_random_uids(self, self.config.neuron.sample_size)

        filtered_axons = [self.metagraph.axons[uid] for uid in available_uids]
        
        ip_per_hotkey = count_hotkeys_per_ip(filtered_axons)
        run_id_per_hotkey = count_run_id_per_hotkey(self.miners_metadata)
        miner_distribution = get_miner_distributions(self.miners_metadata, self.validator_config.get_networks())

        responses = self.dendrite.query(
            filtered_axons,
            protocol.Discovery(),
            deserialize=True,
            timeout = self.validator_config.discovery_timeout,
        )

        valid_uids = []
        valid_responses = []
        for uid, response in zip(available_uids, responses):
            if response and response.output and self.miners_metadata.get(response.axon.hotkey):
                valid_uids.append(uid)
                valid_responses.append(response)

            status_code = response.axon.status_code
            status_message = response.axon.status_message
            if response.is_failure:
                bt.logging.info(f"Skipping response: Failure, miner {response.axon.hotkey} returned {status_code=}: {status_message=}")
            elif response.is_blacklist:
                bt.logging.info(f"Skipping response: Blacklist, miner {response.axon.hotkey} returned {status_code=}: {status_message=}")
            elif response.is_timeout:
                bt.logging.info(f"Skipping response: Timeout, miner {response.axon.hotkey}")

        if valid_responses:
            rewards = [
                self.get_reward(response, 
                                ip_per_hotkey=ip_per_hotkey,
                                run_id_per_hotkey=run_id_per_hotkey,
                                miner_distribution=miner_distribution) for response in valid_responses
            ]
            # Remove None reward as they represent timeout cross validation
            filtered_data = [(reward, uid) for reward, uid in zip(rewards, valid_uids) if reward is not None]

            
            
            if filtered_data:
                rewards, valid_uids = zip(*filtered_data)

                rewards = torch.FloatTensor(rewards)
                self.update_scores(rewards, valid_uids)
            else: 
                bt.logging.info('Skipping update_scores() as no responses were valid')

    def sync_validator(self):
        self.miners_metadata = get_miners_metadata(self.config, self.metagraph)
        self.validator_config = ValidatorConfig().load_and_get_config_values()
        self.scorer = Scorer(self.validator_config)

        self.networks = self.validator_config.get_networks()
        self.block_height_cache = {network: self.nodes[network].get_current_block_height() for network in self.networks}

        validator_uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
        store_validator_metadata(self.config, self.wallet, validator_uid)

    def resync_metagraph(self):
        super(Validator, self).resync_metagraph()
        self.sync_validator()




if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    with Validator() as validator:
        while True:
            bt.logging.info("Validator running")
            time.sleep(bt.__blocktime__*10)


