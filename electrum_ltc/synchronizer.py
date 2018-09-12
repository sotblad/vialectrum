#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2014 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import asyncio
import hashlib

from aiorpcx import TaskGroup

from .transaction import Transaction
from .util import bh2u, PrintError
from .bitcoin import address_to_scripthash


def history_status(h):
    if not h:
        return None
    status = ''
    for tx_hash, height in h:
        status += tx_hash + ':%d:' % height
    return bh2u(hashlib.sha256(status.encode('ascii')).digest())


class Synchronizer(PrintError):
    '''The synchronizer keeps the wallet up-to-date with its set of
    addresses and their transactions.  It subscribes over the network
    to wallet addresses, gets the wallet to generate new addresses
    when necessary, requests the transaction history of any addresses
    we don't have the full history of, and requests binary transaction
    data of any transactions the wallet doesn't have.
    '''
    def __init__(self, wallet):
        self.wallet = wallet
        self.requested_tx = {}
        self.requested_histories = {}
        self.requested_addrs = set()
        self.scripthash_to_address = {}
        # Queues
        self.add_queue = asyncio.Queue()
        self.status_queue = asyncio.Queue()

    def is_up_to_date(self):
        return (not self.requested_addrs
                and not self.requested_histories
                and not self.requested_tx)

    def add(self, addr):
        self.requested_addrs.add(addr)
        self.add_queue.put_nowait(addr)

    async def on_address_status(self, addr, status):
        history = self.wallet.history.get(addr, [])
        if history_status(history) == status:
            return
        # note that at this point 'result' can be None;
        # if we had a history for addr but now the server is telling us
        # there is no history
        if addr in self.requested_histories:
            return
        # request address history
        self.requested_histories[addr] = status
        h = address_to_scripthash(addr)
        result = await self.session.send_request("blockchain.scripthash.get_history", [h])
        self.print_error("receiving history", addr, len(result))
        hashes = set(map(lambda item: item['tx_hash'], result))
        hist = list(map(lambda item: (item['tx_hash'], item['height']), result))
        # tx_fees
        tx_fees = [(item['tx_hash'], item.get('fee')) for item in result]
        tx_fees = dict(filter(lambda x:x[1] is not None, tx_fees))
        # Check that txids are unique
        if len(hashes) != len(result):
            self.print_error("error: server history has non-unique txids: %s"% addr)
        # Check that the status corresponds to what was announced
        elif history_status(hist) != status:
            self.print_error("error: status mismatch: %s" % addr)
        else:
            # Store received history
            self.wallet.receive_history_callback(addr, hist, tx_fees)
            # Request transactions we don't have
            await self.request_missing_txs(hist)

        # Remove request; this allows up_to_date to be True
        self.requested_histories.pop(addr)

        if self.wallet.network: self.wallet.network.notify('updated')

    async def request_missing_txs(self, hist):
        # "hist" is a list of [tx_hash, tx_height] lists
        transaction_hashes = []
        for tx_hash, tx_height in hist:
            if tx_hash in self.requested_tx:
                continue
            if tx_hash in self.wallet.transactions:
                continue
            transaction_hashes.append(tx_hash)
            self.requested_tx[tx_hash] = tx_height

        async with TaskGroup() as group:
            for tx_hash in transaction_hashes:
                await group.spawn(self.get_transaction, tx_hash)

    async def get_transaction(self, tx_hash):
        result = await self.session.send_request('blockchain.transaction.get', [tx_hash])
        tx = Transaction(result)
        try:
            tx.deserialize()
        except Exception:
            self.print_msg("cannot deserialize transaction, skipping", tx_hash)
            return
        if tx_hash != tx.txid():
            self.print_error("received tx does not match expected txid ({} != {})"
                             .format(tx_hash, tx.txid()))
            return
        tx_height = self.requested_tx.pop(tx_hash)
        self.wallet.receive_tx_callback(tx_hash, tx, tx_height)
        self.print_error("received tx %s height: %d bytes: %d" %
                         (tx_hash, tx_height, len(tx.raw)))
        # callbacks
        self.wallet.network.trigger_callback('new_transaction', tx)

    async def subscribe_to_address(self, addr):
        h = address_to_scripthash(addr)
        self.scripthash_to_address[h] = addr
        await self.session.subscribe('blockchain.scripthash.subscribe', [h], self.status_queue)
        self.requested_addrs.remove(addr)

    async def send_subscriptions(self, group: TaskGroup):
        while True:
            addr = await self.add_queue.get()
            await group.spawn(self.subscribe_to_address, addr)

    async def handle_status(self, group: TaskGroup):
        while True:
            h, status = await self.status_queue.get()
            addr = self.scripthash_to_address[h]
            await group.spawn(self.on_address_status, addr, status)

    @property
    def session(self):
        s = self.wallet.network.interface.session
        assert s is not None
        return s

    async def main(self):
        # request missing txns, if any
        async with TaskGroup() as group:
            for history in self.wallet.history.values():
                # Old electrum servers returned ['*'] when all history for the address
                # was pruned. This no longer happens but may remain in old wallets.
                if history == ['*']: continue
                await group.spawn(self.request_missing_txs, history)
        # add addresses to bootstrap
        for addr in self.wallet.get_addresses():
            self.add(addr)
        # main loop
        while True:
            await asyncio.sleep(0.1)
            self.wallet.synchronize()
            up_to_date = self.is_up_to_date()
            if up_to_date != self.wallet.is_up_to_date():
                self.wallet.set_up_to_date(up_to_date)
                self.wallet.network.trigger_callback('updated')
