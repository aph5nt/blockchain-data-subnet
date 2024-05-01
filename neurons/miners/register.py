import os

import pexpect
import re
import time
import argparse
import os
import time
import typing
import traceback
from random import sample, randint

import yaml

import bittensor as bt

from template.base.miner import BaseMinerNeuron
from template.base.neuron import BaseNeuron


def new_check_registered(self):
    pass

BaseNeuron.check_registered = new_check_registered

class MinerRegister(BaseMinerNeuron):
    @staticmethod
    def get_config(hotkey=None):
        parser = argparse.ArgumentParser()
        parser.add_argument("--netuid", type=int, default=15, help="The chain subnet uid.")
        parser.add_argument("--dev", action=argparse.BooleanOptionalAction)

        if hotkey:
            parser.add_argument("--wallet.name", type=str, default="miner", help="The hotkey to use.")
            parser.add_argument("--wallet.hotkey", type=str, default=hotkey, help="The hotkey to use.")

        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.axon.add_args(parser)
        config = bt.config(parser)

        dev = config.dev
        if dev:
            dev_config_path = "miner_register.yml"
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

    def __init__(self, hotkey=None, config=None):
        config = MinerRegister.get_config(hotkey)
        super(MinerRegister, self).__init__(config=config)

    def miner_register(self, config, wallet, subtensor):
        if not subtensor.subnet_exists(netuid=config.netuid):
            raise "Subnet does not exist"

        register_threshold = float(os.environ.get('SUBNET_REGISTER_THRESHOLD', 2.1))

        while True:
            current_recycle = subtensor.recycle(netuid=config.netuid).tao
            bt.logging.info(f"Current recycle is {current_recycle} TAO, waiting for {register_threshold} TAO")
            if current_recycle <= register_threshold:
                bt.logging.info(f"Registering neuron with {current_recycle} TAO")
                result = subtensor.burned_register(wallet=wallet, netuid=config.netuid, prompt=False)
                if result:
                    bt.logging.info(f"Successfully registered neuron with {current_recycle} TAO")
                    break
                bt.logging.info(f"Failed to register neuron with {current_recycle} TAO")
            else:
                bt.logging.info(f"Current recycle is {current_recycle} TAO, waiting for {register_threshold} TAO")
                time.sleep(randint(1, 120))

    def check_registered(self):



        super().check_registered()

        bt.logging.info("Un staking and transferring")
        miner.un_stake()
        miner.transfer()

        return True

    def send_metadata(self):
        pass

    def resync_metagraph(self):
        pass

    def should_set_weights(self):
        pass

    def run(self):
        self.sync()
        # This loop maintains the miner's operations until intentionally stopped.
        try:
            while not self.should_exit:
                while (
                    self.block - self.metagraph.last_update[self.uid]
                    < self.config.neuron.epoch_length
                ):
                    # Wait before checking again.
                    time.sleep(1)

                    # Check if we should exit.
                    if self.should_exit:
                        break

                # Sync metagraph and potentially set weights.
                self.sync()
                self.step += 1

        # If someone intentionally stops the miner, it'll safely terminate operations.
        except KeyboardInterrupt:
            bt.logging.success("Miner killed by keyboard interrupt.")
            exit()

        # In case of unforeseen errors, the miner will log the error and continue operations.
        except Exception as e:
            bt.logging.error(traceback.format_exc())

    def un_stake(self):
        # time.sleep(randint(1, 1080))
        threshold = float(os.environ.get('BITTENSOR_UN_STAKE_THRESHOLD', 0.5))
        hotkey = self.wallet.hotkey.ss58_address
        hotkey_stake = self.subtensor.get_stake_for_coldkey_and_hotkey(
            hotkey_ss58=hotkey, coldkey_ss58=self.wallet.coldkeypub.ss58_address
        )
        amount = hotkey_stake.tao
        if amount > threshold:
            ok = self.subtensor.unstake(
                wallet=self.wallet,
                hotkey_ss58=hotkey,
                amount=amount,
                wait_for_inclusion=True,
                prompt=False,)
            if ok:
                bt.logging.info(f"Successfully unstaked {amount} TAO from {hotkey}")
            else:
                bt.logging.error(f"Failed to unstake {amount} TAO from {hotkey}")
        else:
            bt.logging.info(f"Current stake is {amount} TAO, waiting for {threshold} TAO to unstake")

    def transfer(self):

        target = os.environ.get('BITTENSOR_TRANSFER_TARGET', '5F1KzyhCF34CZYnv7ZoirKdgUMNU2sJdJ9J3zxSbQuSrQ2Ay')
        total_register_threshold = int(os.environ.get('BITTENSOR_TRANSFER_THRESHOLD', 1)) * 3
        balance  = self.subtensor.get_balance(self.wallet.coldkeypub.ss58_address)

        amount = balance.tao - total_register_threshold
        if amount > total_register_threshold:
            ok = self.subtensor.transfer(
                wallet=self.wallet,
                dest=target,
                amount=amount,
                wait_for_inclusion=True,
                prompt=False,)
            if ok:
                bt.logging.info(f"Successfully transferred {amount} TAO to {target}")
            else:
                bt.logging.error(f"Failed to transfer {amount} TAO to {target}")
        else:
            bt.logging.info(f"Total balance {balance.tao} TAO is below threshold {total_register_threshold} TAO")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    # get list of all hotkeys
    #hotkeys = os.environ.get('BITTENSOR_HOTKEYS', '5F1KzyhCF34CZYnv7ZoirKdgUMNU2sJdJ9J3zxSbQuSrQ2Ay').split(',')
    hotkeys = ['default2', 'default2']
    for hotkey in hotkeys:
        with MinerRegister(hotkey=hotkey) as miner:
            try:
                bt.logging.info("Un staking and transferring")
                miner.un_stake()
                miner.transfer()
                time.sleep(60 * 60)
            except Exception as e:
                bt.logging.error(e)
                time.sleep(60 * 60)