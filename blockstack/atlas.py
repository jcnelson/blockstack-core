#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstack
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

    This file is part of Blockstack

    Blockstack is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import sys
import time
import sqlite3
import urllib2
import simplejson
import threading
import random
import struct
import base64
import shutil
import traceback
import copy
import binascii

import virtualchain
log = virtualchain.get_logger("blockstack-server")

from lib.config import *
from lib.storage import *
from lib import get_db_state

from pybloom_live import ScalableBloomFilter

PEER_LIFETIME_INTERVAL = 3600  # 1 hour
PEER_PING_INTERVAL = 60        # 1 minute

MIN_PEER_HEALTH = 0.5  # minimum peer health before we forget about it

NUM_NEIGHBORS = 80     # number of neighbors a peer can report

ZONEFILE_INV = ""      # this atlas peer's current zonefile inventory

MAX_QUEUED_ZONEFILES = 1000     # maximum number of queued zonefiles

if os.environ.get("BLOCKSTACK_ATLAS_PEER_LIFETIME") is not None:
    PEER_LIFETIME_INTERVAL = int(os.environ.get("BLOCKSTACK_ATLAS_PEER_LIFETIME"))

if os.environ.get("BLOCKSTACK_ATLAS_PEER_PING_INTERVAL") is not None:
    PEER_PING_INTERVAL = int(os.environ.get("BLOCKSTACK_ATLAS_PEER_PING_INTERVAL"))

if os.environ.get("BLOCKSTACK_ATLAS_MIN_PEER_HEALTH") is not None:
    MIN_PEER_HEALTH = float(os.environ.get("BLOCKSTACK_ATLAS_MIN_PEER_HEALTH"))

if os.environ.get("BLOCKSTACK_ATLAS_NUM_NEIGHBORS") is not None:
    NUM_NEIGHBORS = int(os.environ.get("BLOCKSTACK_ATLAS_NUM_NEIGHBORS"))

if os.environ.get("BLOCKSTACK_ATLAS_MAX_NEIGHBORS") is not None:
    NUM_NEIGHBORS = int(os.environ.get("BLOCKSTACK_ATLAS_MAX_NEIGHBORS"))

if os.environ.get("BLOCKSTACK_TEST", None) == "1" and os.environ.get("BLOCKSTACK_ATLAS_UNIT_TEST", None) is None:
    # use test client
    from blockstack_integration_tests import AtlasRPCTestClient as BlockstackRPCClient
    from blockstack_integration_tests import time_now, time_sleep, atlas_max_neighbors, atlas_peer_lifetime_interval, atlas_peer_ping_interval

else:
    # production
    from blockstack_client import BlockstackRPCClient
    
    def time_now():
        return time.time()

    def time_sleep(hostport, procname, value):
        return time.sleep(value)
    
    def atlas_max_neighbors():
        return NUM_NEIGHBORS

    def atlas_peer_lifetime_interval():
        return PEER_LIFETIME_INTERVAL

    def atlas_peer_ping_interval():
        return PEER_PING_INTERVAL


ATLASDB_SQL = """
CREATE TABLE zonefiles( inv_index INTEGER PRIMARY KEY AUTOINCREMENT,
                        zonefile_hash STRING NOT NULL,
                        present INTEGER NOT NULL,
                        block_height INTEGER NOT NULL );
"""

PEER_TABLE = {}        # map peer host:port (NOT url) to peer information
                       # each element is {'time': [(responded, timestamp)...], 'popularity': ..., 'popularity_bloom': ..., 'zonefile_lastblock': ..., 'zonefile_inv': ...}
                       # 'zonefile_inv' is a *bitwise big-endian* bit string where bit i is set if the zonefile in the ith NAME_UPDATE transaction has been stored by us (i.e. "is present")
                       # for example, if 'zonefile_inv' is 10110001, then the 0th, 2nd, 3rd, and 7th NAME_UPDATEs' zonefiles have been stored by us
                       # (note that we allow for the possibility of duplicate zonefiles, but this is a rare occurance and we keep track of it in the DB to avoid duplicate transfers)

PEER_QUEUE = []        # list of peers (host:port) to begin talking to, discovered via the Atlas RPC interface
ZONEFILE_QUEUE = []    # list of {zonefile_hash: zonefile} dicts to push out to other Atlas nodes (i.e. received from clients)

PEER_TABLE_LOCK = threading.Lock()
PEER_QUEUE_LOCK = threading.Lock()
ZONEFILE_QUEUE_LOCK = threading.Lock()

def atlas_peer_table_lock():
    """
    Lock the global health info table.
    Return the table.
    """
    global PEER_TABLE_LOCK, PEER_TABLE
    PEER_TABLE_LOCK.acquire()
    return PEER_TABLE


def atlas_peer_table_unlock():
    """
    Unlock the global health info table.
    """
    global PEER_TABLE_LOCK
    PEER_TABLE_LOCK.release()
    return


def atlas_peer_queue_lock():
    """
    Lock the global peer queue
    return the queue
    """
    global PEER_QUEUE_LOCK, PEER_QUEUE
    PEER_QUEUE_LOCK.acquire()
    return PEER_QUEUE


def atlas_peer_queue_unlock():
    """
    Unlock the global peer queue
    """
    global PEER_QUEUE_LOCK
    PEER_QUEUE_LOCK.release()


def atlas_zonefile_queue_lock():
    """
    Lock the global zonefile queue
    return the queue
    """
    global ZONEFILE_QUEUE_LOCK
    ZONEFILE_QUEUE_LOCK.acquire()
    return ZONEFILE_QUEUE


def atlas_zonefile_queue_unlock():
    """
    Unlock the global zonefile queue
    """
    global ZONEFILE_QUEUE_LOCK
    ZONEFILE_QUEUE_LOCK.release()


def atlas_inventory_flip_zonefile_bits( inv_vec, bit_indexes, operation ):
    """
    Given a list of bit indexes (bit_indexes), set or clear the
    appropriate bits in the inventory vector (inv_vec).
    If the bit index is beyond the length of inv_vec, 
    then expand inv_vec to accomodate it.

    If operation is True, then set the bits.
    If operation is False, then clear the bits

    Return the new inv_vec
    """
    inv_list = list(inv_vec)

    max_byte_index = max(bit_indexes) / 8 + 1
    if len(inv_list) <= max_byte_index:
        inv_list += ['\0'] * (max_byte_index - len(inv_list))

    for bit_index in bit_indexes:
        byte_index = bit_index / 8
        bit_index = 7 - (bit_index % 8)
        
        zfbits = ord(inv_list[byte_index])

        if operation:
            zfbits = zfbits | (1 << bit_index)
        else:
            zfbits = zfbits & ~(1 << bit_index)

        inv_list[byte_index] = chr(zfbits)

    return "".join(inv_list)


def atlas_inventory_set_zonefile_bits( inv_vec, bit_indexes ):
    """
    Set bits in a zonefile inventory vector.
    """
    return atlas_inventory_flip_zonefile_bits( inv_vec, bit_indexes, True )


def atlas_inventory_clear_zonefile_bits( inv_vec, bit_indexes ):
    """
    Clear bits in a zonefile inventory vector
    """
    return atlas_inventory_flip_zonefile_bits( inv_vec, bit_indexes, False )


def atlas_inventory_test_zonefile_bits( inv_vec, bit_indexes ):
    """
    Given a list of bit indexes (bit_indexes), determine whether or not 
    they are set.

    Return True if all are set
    Return False if not
    """
    inv_list = list(inv_vec)

    max_byte_index = max(bit_indexes) / 8 + 1
    if len(inv_list) <= max_byte_index:
        inv_list += ['\0'] * (max_byte_index - len(inv_list))

    ret = True
    for bit_index in bit_indexes:
        byte_index = bit_index / 8
        bit_index = 7 - (bit_index % 8)
        
        zfbits = ord(inv_list[byte_index])
        ret = (ret and ((zfbits & (1 << bit_index)) != 0))

    return ret


def atlasdb_row_factory( cursor, row ):
    """
    row factory
    * convert known booleans to booleans
    """
    d = {}
    for idx, col in enumerate( cursor.description ):
        if col[0] == 'present':
            if row[idx] == 0:
                d[col[0]] = False
            elif row[idx] == 1:
                d[col[0]] = True
            else:
                raise Exception("Invalid value for 'present': %s" % row[idx])

        else:
            d[col[0]] = row[idx]

    return d


def atlasdb_path( impl=None ):
    """
    Get the path to the atlas DB
    """
    working_dir = virtualchain.get_working_dir(impl=impl)
    return os.path.join(working_dir, "atlas.db")


def atlasdb_query_execute( cur, query, values ):
    """
    Execute a query.  If it fails, exit.

    DO NOT CALL THIS DIRECTLY.
    """

    try:
        ret = cur.execute( query, values )
        return ret
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to execute query (%s, %s)" % (query, values))
        log.error("\n" + "\n".join(traceback.format_stack()))
        sys.exit(1)


def atlasdb_open( path ):
    """
    Open the atlas db.
    Return a connection.
    Return None if it doesn't exist
    """
    if not os.path.exists(path):
        log.debug("Atlas DB doesn't exist at %s" % path)
        return None

    con = sqlite3.connect( path, isolation_level=None )
    con.row_factory = atlasdb_row_factory
    return con


def atlasdb_add_zonefile_info( zonefile_hash, present, block_height, con=None, path=None ):
    """
    Add a zonefile to the database.
    Mark it as present or absent.
    Keep our in-RAM inventory vector up-to-date
    """
    global ZONEFILE_INV

    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "INSERT INTO zonefiles (zonefile_hash, present, block_height) VALUES (?,?,?);"
    args = (zonefile_hash, present, block_height)

    cur = con.cursor()
    atlasdb_query_execute( cur, sql, args )
    con.commit()

    # keep in-RAM zonefile inv coherent
    zfbits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )
    ZONEFILE_INV = atlas_inventory_set_zonefile_bits( ZONEFILE_INV, zfbits )

    if close:
        con.close()

    return True


def atlasdb_get_zonefile( zonefile_hash, con=None, path=None ):
    """
    Look up all information on this zonefile.
    Returns {'zonefile_hash': ..., 'indexes': [...], etc}
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT * FROM zonefiles WHERE zonefile_hash = ?;"
    args = (zonefile_hash,)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = {
        'zonefile_hash': zonefile_hash,
        'indexes': [],
        'block_heights': [],
        'present': None
    }

    for zfinfo in res:
        ret['indexes'].append( zfinfo['inv_index'] )
        ret['block_heights'].append( zfinfo['block_height'] )
        ret['present'] = zfinfo['present']

    if close:
        con.close()

    return ret


def atlasdb_set_zonefile_present( zonefile_hash, present, con=None, path=None ):
    """
    Mark a zonefile as present (i.e. we stored it).
    Keep our in-RAM zonefile inventory coherent.
    Return the previous state.
    """
    global ZONEFILE_INV

    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    if present:
        present = 1
    else:
        present = 0

    sql = "UPDATE zonefiles SET present = ? WHERE zonefile_hash = ?;"
    args = (present, zonefile_hash)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    zfbits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )
    
    # did we know about this?
    was_present = atlas_inventory_test_zonefile_bits( ZONEFILE_INV, zfbits )

    # keep our inventory vector coherent.
    ZONEFILE_INV = atlas_inventory_flip_zonefile_bits( ZONEFILE_INV, zfbits, present )

    if close:
        con.close()

    return was_present


def atlasdb_get_zonefile_bits( zonefile_hash, con=None, path=None ):
    """
    What bit(s) in a zonefile inventory does a zonefile hash correspond to?
    Return their indexes in the bit field.
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT inv_index FROM zonefiles WHERE zonefile_hash = ?;"
    args = (zonefile_hash,)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    # NOTE: zero-indexed
    ret = [r['inv_index'] - 1 for r in res]

    if close:
        con.close()

    return ret


def atlasdb_queue_zonefiles( con, db, start_block, zonefile_dir=None, validate=True ):
    """
    Queue all zonefile hashes in the BlockstackDB
    to the zonefile queue
    """
    # populate zonefile queue
    total = 0
    for block_height in xrange(start_block, db.lastblock+1, 1 ):

        zonefile_hashes = db.get_value_hashes_at( block_height )
        for zfhash in zonefile_hashes:
            present = is_zonefile_cached( zfhash, zonefile_dir=zonefile_dir, validate=validate ) 
            atlasdb_add_zonefile_info( zfhash, present, block_height, con=con )
            total += 1

    log.debug("Queued %s zonefiles from %s-%s" % (total, start_block, db.lastblock))
    return True


def atlasdb_init( path, db, peer_seeds, peer_blacklist, validate=False, zonefile_dir=None ):
    """
    Set up the atlas node:
    * create the db if it doesn't exist
    * go through all the names and verify that we have the *current* zonefiles
    * if we don't, queue them for fetching.
    * set up the peer db

    @db should be an instance of BlockstackDB
    @initial_peers should be a list of URLs

    Return the newly-initialized peer table
    """
    
    global ATLASDB_SQL
    global PEER_TABLE

    if os.path.exists( path ):
        log.debug("Atlas DB exists at %s" % path)

    else:

        log.debug("Initializing Atlas DB at %s" % path)

        lines = [l + ";" for l in ATLASDB_SQL.split(";")]
        con = sqlite3.connect( path, isolation_level=None )

        for line in lines:
            con.execute(line)

        con.row_factory = atlasdb_row_factory

        # populate from db
        log.debug("Queuing all zonefiles")
        atlasdb_queue_zonefiles( con, db, FIRST_BLOCK_MAINNET, validate=validate, zonefile_dir=zonefile_dir )
        con.close()

    peer_table = {}

    # add initial peer info
    for peer_url in peer_seeds + peer_blacklist:
        host, port = url_to_host_port( peer_url )
        peer_hostport = "%s:%s" % (host, port)

        atlas_init_peer_info( peer_table, peer_hostport )

        if peer_url in peer_blacklist:
            peer_table[peer_hostport]['blacklisted'] = True

    return peer_table


def atlasdb_zonefile_info_list( start, end, con=None, path=None ):
    """
    Get a listing of zonefile information
    for a given blockchain range [start, end].
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT * FROM zonefiles WHERE block_height >= ? AND block_height <= ?;"
    args = (start, end)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = []
    for row in res:
        tmp = {}
        tmp.update(row)
        ret.append(tmp)

    if close:
        con.close()

    return ret


def atlasdb_zonefile_find_missing( offset, count, con=None, path=None ):
    """
    Find out which zonefiles we're still missing.
    Return a list of zonefile rows, where present == 0.

    This is slow.  Use the in-RAM zonefile inventory vector whenever possible.
    (see atlas_inventory_find_missing)
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT * FROM zonefiles WHERE present = 0 OFFSET ? LIMIT ?;"
    args = (start, end)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = []
    for row in res:
        tmp = {}
        tmp.update(row)
        ret.append(tmp)

    if close:
        con.close()

    return ret


def atlas_make_zonefile_inventory( start, end, con=None, path=None ):
    """
    Get a summary description of the list of zonefiles we have
    for the given block range (a "zonefile inventory")

    Zonefile present/absent bits are ordered left-to-right,
    where the leftmost bit is the earliest zonefile in the blockchain.

    This is slow.  Use the in-RAM zonefile inventory vector whenever possible
    (see atlas_get_inventory).
    """
    
    listing = atlasdb_zonefile_info_list( start, end, con=con, path=path )

    # group by block height, order by tx
    tmp = {}
    for row in listing:
        if not tmp.has_key(row['block_height']):
            tmp[row['block_height']] = []

        tmp[row['block_height']].append( row['present'] )

    # serialize
    bool_vec = []
    for height in sorted(tmp.keys()):
        bool_vec += tmp[height]

    if len(bool_vec) % 8 != 0: 
        # pad 
        bool_vec += [False] * (8 - (len(bool_vec) % 8))

    inv = ""
    for i in xrange(0, len(bool_vec), 8):
        bit_vec = map( lambda b: 1 if b else 0, bool_vec[i:i+8] )
        next_byte = (bit_vec[0] << 7) | \
                    (bit_vec[1] << 6) | \
                    (bit_vec[2] << 5) | \
                    (bit_vec[3] << 4) | \
                    (bit_vec[4] << 3) | \
                    (bit_vec[5] << 2) | \
                    (bit_vec[6] << 1) | \
                    (bit_vec[7])
        inv += chr(next_byte)

    return inv


def atlas_inventory_find_missing( offset, count, zonefile_inv=None ):
    """
    Find the missing zonefile bits.
    Use the global zonefile inventory vector by default,
    or optionally the given zonefile_inv.
    """
    if zonefile_inv is None:
        zonefile_inv = atlas_get_zonefile_inventory()

    bits = []
    for i in xrange(offset, offset+count):
        byte_offset = i / 8
        bit_offset = 7 - (i % 8)
        if byte_offset >= len(zonefile_inv):
            # beyond the length of this inv
            bits.append(i)
        else:
            is_set = ord(zonefile_inv[byte_index]) & (1 << bit_index)
            if is_set == 0:
                # not set 
                bits.append(i)

    return bits


def atlas_get_zonefile_inventory():
    """
    Get the in-RAM zonefile inventory vector.
    """
    global ZONEFILE_INV
    return ZONEFILE_INV


def atlas_init_peer_info( peer_table, peer_hostport ):
    """
    Initialize peer info table entry
    """
    peer_table[peer_hostport] = {
        "popularity": 1,
        "popularity_bloom": ScalableBloomFilter(),
        "time": [],
        "zonefile_lastblock": FIRST_BLOCK_MAINNET,
        "zonefile_inv": ""
    }


def url_to_host_port( url, port=RPC_SERVER_PORT ):
    """
    Given a URL, turn it into (host, port).
    Return (None, None) on invalid URL
    """
    urlinfo = urllib2.urlparse.urlparse(url)
    hostport = urlinfo.netloc

    parts = hostport.split("@")
    if len(parts) > 2:
        return (None, None)

    if len(parts) == 2:
        hostport = parts[1]

    parts = hostport.split(":")
    if len(parts) > 2:
        return (None, None)

    if len(parts) == 2:
        try:
            port = int(parts[1])
        except:
            return (None, None)

    return parts[0], port


def atlas_peer_is_live( peer_hostport, peer_table, min_health=MIN_PEER_HEALTH ):
    """
    Have we heard from this node recently?
    """
    if not peer_table.has_key(peer_hostport):
        return False

    health_score = atlas_peer_get_health( peer_hostport, peer_table=peer_table )
    return health_score > min_health


def atlas_peer_ping( peer_hostport, timeout=3 ):
    """
    Ping a host
    Return True if alive
    Return False if not
    """
    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout )

    log.debug("Ping %s" % peer_hostport)
    try:
        rpc.ping()
        return True
    except:
        return False


def atlas_get_rarest_live_peers( peer_table=None, min_health=MIN_PEER_HEALTH ):
    """
    Get the list of peers we've heard from recently.
    Use the global peer health table if no health info is given.
    Rank peers by rarest-first.
    """

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()
        
    rarity_rank = []    # (popularity, peer_hostport)
    for peer_hostport in peer_table.keys():
        if atlas_peer_is_live( peer_hostport, peer_table, min_health=min_health ):
            # have recently seen
            rarity_rank.append( (peer_table[peer_hostport]['popularity'], peer_hostport) )

    rarity_rank.sort()  # sorts to least-popular first
    if locked:
        atlas_peer_table_unlock()

    return [peer_hostport for _, peer_hostport in rarity_rank]


def atlas_remove_peers( dead_peers, peer_table ):
    """
    Remove all peer information for the given dead peers from the given health info,
    as well as from the db.
    Only preserve unconditionally if we've blacklisted them
    explicitly.

    Return the new health info.
    """

    for peer_hostport in dead_peers:
        if peer_table.has_key(peer_hostport):
            if peer_table[peer_hostport].get("blacklisted", False):
                continue

            log.debug("Forget peer '%s'" % dead_peers)
            del peer_table[peer_hostport]

    return peer_table


def atlas_peer_get_health( peer_hostport, peer_table=None ):
    """
    Get the health score for a peer.
    Health is: (number of responses received / number of requests sent) 
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    # availability score: number of responses / number of requests
    num_responses = 0
    num_requests = 0
    for (t, r) in peer_table[peer_hostport]['time']:
        num_requests += 1
        if r:
            num_responses += 1

    availability_score = 0.0
    if num_requests > 0:
        availability_score = float(num_responses) / float(num_requests)

    if locked:
        atlas_peer_table_unlock()

    return availability_score


def atlas_peer_update_health( peer_hostport, received_response, peer_table=None ):
    """
    Mark the given peer as alive at this time.
    Update times at which we contacted it,
    and update its health score.

    Use the global health table by default, 
    or use the given health info if set.
    """

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    # if blacklisted, then we don't care 
    if peer_table[peer_hostport].get("blacklisted", False):
        if locked:
            atlas_peer_table_unlock()

        return True

    # record that we contacted this peer, and whether or not we useful info from it
    now = time_now()

    # update timestamps; remove old data
    new_times = []
    for (t, r) in peer_table[peer_hostport]['time']:
        if t + atlas_peer_lifetime_interval() < now:
            continue
        
        new_times.append((t, r))

    new_times.append((now, received_response))
    peer_table[peer_hostport]['time'] = new_times

    if locked:
        atlas_peer_table_unlock()

    return True


def atlas_peer_add_neighbor( peer_hostport, peer_neighbor_hostport, peer_table=None ):
    """
    Record that peer_hostport knows about peer_neighbor_hostport (if we haven't done so already).
    Add the peers to the peer table if they're not there now.
    Use the global peer table if the given peer table is None.

    This method is idempotent for fixed peer_table values
    """
    
    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        atlas_init_peer_info( peer_table, new_hostport )

    if peer_neighbor_hostport not in peer_table.keys():
        atlas_init_peer_info( peer_table, peer_neighbor_hostport )

    if peer_neighbor_hostport not in peer_table[peer_hostport]['popularity_bloom']:
        # we didn't know that peer_hostport knew about peer_neighbor_hostport
        peer_table[peer_neighbor_hostport]['popularity'] += 1
        peer_table[peer_hostport]['popularity_bloom'].add( peer_neighbor_hostport )

    if locked:
        atlas_peer_table_unlock()

    return


def atlas_peer_get_zonefile_inventory_range( peer_hostport, startblock, lastblock, timeout=10, peer_table=None ):
    """
    Get the zonefile inventory bit vector for a given peer.
    startblock and lastblock are inclusive.

    Update peer health information as well.

    Return the bit vector on success.
    Return None if we couldn't contact the peer.
    """

    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout )

    zf_inv = {}
    zf_inv_list = None

    try:
        zf_inv = rpc.get_zonefile_inventory( start, end )
        
        # sanity check
        assert type(zf_inv) == dict, "Inventory is not a dict"
        if 'error' not in zf_inv.keys():
            assert 'status' in zf_inv, "Invalid inv reply"
            assert zf_inv['status'], "Invalid inv reply"
            assert 'inv' in zf_inv, "Invalid inv reply"
            assert type(zf_inv['inv']) in [str, unicode], "Invalid inv bit field"
            zf_inv['inv'] = base64.b64decode( str(zf_inv['inv']) )

            # success!
            atlas_peer_update_health( peer_hostport, True, peer_table=peer_table )

        else:
            assert type(zf_inv['error']) in [str, unicode], "Invalid error message"

    except Exception, e:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(e)

        log.error("Failed to ask %s for zonefile inventory over %s-%s" % (peer_hostport, start, end))
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None

    if 'error' in zf_inv:
        log.error("Failed to get inventory for %s-%s from %s: %s" % (start, end, peer_hostport, zf_inv['error']))

        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None

    atlas_peer_update_health( peer_hostport, True, peer_table=peer_table )
    return zf_inv['inv']


def atlas_peer_sync_zonefile_inventory( peer_hostport, lastblock, timeout=10, peer_table=None ):
    """
    Synchronize our knowledge of a peer's zonefiles up to a given block height.
    NOT THREAD SAFE; CALL FROM ONLY ONE THREAD.

    Return the new inv vector if we synced it
    Return None if not
    """
    peer_inv = ""
    interval = 10000

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    first_block = peer_table[peer_hostport]['zonefile_lastblock']
    peer_inv = peer_table[peerhostport]['zonefile_inv']

    if locked:
        atlas_peer_table_unlock()

    if first_block >= lastblock:
        # synced already
        return peer_inv

    for height in xrange( first_block, lastblock, interval):

        maxheight = min(lastblock, height+interval)
        next_inv = atlas_peer_get_zonefile_inventory_range( peer_hostport, height, maxheight, timeout=timeout, peer_table=peer_table )
        if next_inv is None:
            # partial failure
            log.debug("Failed to sync inventory for %s from %s to %s" % (peer_hostport, height, maxheight))
            break

        peer_inv += next_inv
   
    if locked:
        peer_table = atlas_peer_table_lock()

    peer_table[peer_hostport]['zonefile_inv'] = peer_inv
    peer_table[peer_hostport]['zonefile_lastblock'] = maxheight

    if locked:
        atlas_peer_table_unlock()

    return peer_inv


def atlas_peer_refresh_zonefile_inventory( peer_hostport, firstblock, timeout=10, peer_table=None, con=None, path=None ):
    """
    Refresh a peer's zonefile recent inventory vector entries,
    by removing every bit after firstblock and re-synchronizing them.

    The intuition here is that recent zonefiles are much rarer than older
    zonefiles (which will have been near-100% replicated), meaning the tail
    of the peer's zonefile inventory is a lot less stable than the head (since
    peers will be actively distributing recent zonefiles).

    NOT THREAD SAFE; CALL FROM ONLY ONE THREAD.

    Return True if we synced all the way up to lastblock
    Return False if not.
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    # reset the peer's zonefile inventory, back to firstblock
    zfinfo = atlasdb_zonefile_info_list( FIRST_BLOCK_MAINNET, firstblock, con=con, path=path)
    offset = len(zfinfo)

    cur_inv = peer_table[peer_hostport]['zonefile_inventory']
    peer_table[peer_hostport]['zonefile_lastblock'] = firstblock
    peer_table[peer_hostport]['zonefile_inventory'] = cur_inv[:offset]

    inv = atlas_peer_sync_zonefile_inventory( peer_hostport, lastblock, timeout=timeout, peer_table=peer_table )

    if inv is not None:
        # success!
        peer_table[peer_hostport]['zonefile_inventory_last_refresh'] = time_now()

    if locked:
        atlas_peer_table_unlock()

    if inv is None:
        return False

    else:
        return True


def atlas_peer_has_fresh_zonefile_inventory( peer_hostport, peer_table=None ):
    """
    Does the given atlas node have a fresh zonefile inventory?
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    fresh = False
    now = time_now()
    if peer_table[peer_hostport].has_key('zonefile_inventory_last_refresh') and peer_table[peer_hostport]['zonefile_inventory_last_fresh'] + atlas_peer_ping_interval() >= now:
        fresh = True

    if locked:
        atlas_peer_table_unlock()

    return fresh


def atlas_peer_set_zonefile_status( peer_hostport, zonefile_hash, present, zonefile_bits=None, peer_table=None, con=None, path=None ):
    """
    Mark a zonefile as being present or absent on a peer.
    Use this method to update our knowledge of what other peers have,
    based on when we try to ask them for zonefiles (i.e. a peer can
    lie about what zonefiles it has, and if it advertizes the availability
    of a zonefile but doesn't deliver, then we need to remember not
    to ask it again).
    """
    if zonefile_bits is None:
        zonefile_bits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_table.has_key(peer_hostport):
        peer_inv = peer_table[peer_hostport]['zonefile_inv']
        peer_inv = atlas_inventory_flip_zonefile_bits( peer_inv, zonefile_bits, present )
        peer_table[peer_hostport]['zonefile_inv'] = peer_inv
                
    if locked:
        atlas_peer_table_unlock()

    return


def atlas_find_missing_zonefile_availability( peer_table=None, con=None, path=None, missing_zonefile_info=None ):
    """
    Find the set of missing zonefiles, as well as their popularity amongst 
    our neighbors.

    Only consider zonefiles that are known by at least
    one peer; otherwise they're missing from
    our clique (and we'll re-sync our neighborss' inventories
    every so often to make sure we detect when zonefiles
    become available).

    Return a dict, structured as:
    {
        'zonefile hash': {
            'indexes': [...],
            'popularity': ...,
            'peers': [...]
        }
    }
    """

    # which zonefiles do we have?
    offset = 0
    count = 10000
    missing = []
    ret = {}

    if missing_zonefile_info is None:
        while True:
            # TODO: use in-RAM coherent copy of our zonefile inventory
            zfinfo = atlasdb_zonefile_find_missing( offset, count, con=con, path=path )
            if len(zfinfo) == 0:
                break

            missing += zfinfo
            offset += len(zfinfo)

    else:
        missing = missing_zonefile_info

    if len(missing) == 0:
        # none!
        return []

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    # do any other peers have this zonefile?
    for zfinfo in missing:
        popularity = 0
        byte_index = zfinfo['inv_index'] / 8
        bit_index = 7 - (zfinfo['inv_index'] % 8)
        peers = []

        if not ret.has_key(zfinfo['zonefile_hash']):
            ret[zfinfo['zonefile_hash']] = {
                'indexes': [],
                'popularity': 0,
                'peers': []
            }

        for peer_hostport in peer_table.keys():
            if len(peer_table[peer_hostport]['zonefile_inv']) <= byte_index:
                # too new for this peer
                continue

            if (ord(peer_table[peer_hostport]['zonefile_inv'][byte_index]) & (1 << bit_index)) == 0:
                # this peer doesn't have it
                continue

            if peer_hostport not in ret[zfinfo['zonefile_hash']]['peers']:
                popularity += 1
                peers.append( peer_hostport )


        ret[zfinfo['zonefile_hash']]['indexes'].append( zfinfo['inv_index'] )
        ret[zfinfo['zonefile_hash']]['popularity'] += popularity
        ret[zfinfo['zonefile_hash']]['peers'] += peers

    if locked:
        atlas_peer_table_unlock()

    return ret


def atlas_peer_has_zonefile( peer_hostport, zonefile_hash, zonefile_bits=None, con=None, path=None, peer_table=None ):
    """
    Does the given peer have the given zonefile defined?
    Check its inventory vector

    Return True if present
    Return False if not present
    Return None if we don't know about the zonefile ourselves
    """

    bits = None
    if zonefile_bits is None:
        bits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )
        if len(bits) == 0:
            return None

    else:
        bits = zonefile_bits

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    zonefile_inv = peer_table[peer_hostport]['zonefile_inv']
    
    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    res = atlas_inventory_test_zonefile_bits( zonefile_inv, bits )
    return res


def atlas_peer_get_neighbors( my_hostport, peer_hostport, timeout=10, peer_table=None ):
    """
    Ask the peer server at the given URL for its
    K-rarest peers.

    Update the health info in peer_table
    (if not given, the global peer table will be used instead)

    Return the list on success
    Return None on failure to contact
    Raise on invalid URL
    """
   
    host, port = url_to_host_port( peer_hostport )
    if host is None or port is None:
        log.debug("Invalid host/port %s" % peer_hostport)
        raise ValueError("Invalid host/port %s" % peer_hostport)

    rpc = BlockstackRPCClient( host, port, timeout=timeout )
    
    try:
        peer_list = rpc.get_atlas_peers( my_hostport )

        assert type(peer_list) in [dict], "Not a peer list response"

        if 'error' not in peer_list:
            assert 'status' in peer_list, "No status in response"
            assert 'peers' in peer_list, "No peers in response"
            assert type(peer_list['peers']) in [list], "Not a peer list"
            for peer in peer_list['peers']:
                assert type(peer) in [str, unicode], "Invalid peer list"

            # sane limits
            max_neighbors = atlas_max_neighbors()
            if len(peer_list['peers']) > max_neighbors:
                peer_list['peers'] = peer_list['peers'][:max_neighbors]

        else:
            assert type(peer_list['error']) in [str, unicode], "Invalid error message"

    except AssertionError, ae:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(ae)
        log.error("Invalid peer list response from '%s'" % peer_hostport)
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None

    except Exception, e:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(e)
        log.error("Failed to talk to '%s'" % peer_hostport)
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None

    if 'error' in peer_list:
        log.debug("Remote peer error: %s" % peer_list['error'])
        log.error("Remote peer error")
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None

    ret = peer_list['peers']
    atlas_peer_update_health( peer_hostport, True, peer_table=peer_table )
    return ret


def atlas_get_zonefiles( peer_hostport, zonefile_hashes, timeout=60, peer_table=None ):
    """
    Given a list of zonefile hashes.
    go and get them from the given host.

    Update node health

    Return the newly-fetched zonefiles on success (as a dict mapping hashes to zonefile data)
    Return None on error.
    """

    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout )

    try:
        zf_data = rpc.get_zonefiles( zonefile_hashes )
        assert type(zf_data) == dict, "Invalid zonefile listing"
        if 'error' not in zf_data.keys():
            assert 'status' in zf_data.keys(), "Invalid zonefile reply"
            assert zf_data['status'], "Invalid zonefile reply"

            assert 'zonefiles' in zf_data.keys(), "No zonefiles"
            zonefiles = zf_data['zonefiles']

            assert type(zonefiles) == dict, "Invalid zonefiles"
            for zf_hash in zonefiles.keys():
                assert type(zf_hash) in [str, unicode], "Invalid zonefile hash"
                assert len(zf_hash) == LENGTHS['value_hash'], "Invalid zonefile hash length"

                assert type(zonefile[zf_hash]) in [str, unicode], "Invalid zonefile data"
                assert is_valid_zonefile( zonefile[zf_hash], zf_hash ), "Zonefile does not match or is not current" 

        else:
            assert type(zf_data['error']) in [str, unicode], "Invalid error message"

    except Exception, e:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(e)

        log.error("Invalid zonefile data from %s" % peer_hostport)

        # unpopular
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None 

    if 'error' in zf_data.keys():
        log.error("Failed to fetch zonefile data from %s: %s" % (peer_hostport, zf_data['error']))
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None 

    atlas_peer_update_health( peer_hostport, True, peer_table=peer_table )
    return zf_data['zonefiles']


def atlas_rank_peers_by_health( peer_list=None, peer_table=None ):
    """
    Get a ranking of peers to contact for a zonefile.
    Peers are ranked by health (i.e. availability ratio).
    Only return peers we've talked to (i.e. peers that have
    known availability scores).

    This is used to select peers to serve a particular zonefile.
    """

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_list is None:
        peer_list = peer_table.keys()[:]

    peer_health_ranking = []    # (health score, peer hostport)
    for peer_hostport in peer_list:
        health_score = atlas_peer_get_health( peer_hostport, peer_table=peer_table)
        if health_score > 0:
            peer_health_ranking.append( (health_score, peer_hostport) )
    
    if locked:
        atlas_peer_table_unlock()

    # sort on health
    peer_health_ranking.sort()
    peer_health_ranking.reverse()

    return [peer_hp for _, peer_hp in peer_health_ranking]


def atlas_rank_peers_by_availability( lastblock=None, peer_list=None, peer_table=None, min_health=MIN_PEER_HEALTH, local_inv=None, con=None, path=None ):
    """
    Get a ranking of peers to contact for a zonefile.
    Peers are ranked by the number of zonefiles they have
    which we don't have.  Peers with a lower-than-minimal
    health score are ignored.

    This is used to select neighbors.
    """

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_list is None:
        peer_list = peer_table.keys()[:]

    if local_inv is None:
        # what's my inventory?
        assert lastblock is not None, "Need lastblock or local_inv"
        local_inv = atlas_make_zonefile_availability( FIRST_BLOCK_MAINNET, lastblock, con=con, path=path )

    peer_availability_ranking = []    # (health score, peer hostport)
    for peer_hostport in peer_list:
        health_score = atlas_peer_get_health( peer_hostport, local_inv, peer_table=peer_table)
        if health_score < min_health:
            continue

        availability_score = atlas_peer_get_availability( local_inv, peer_table[peer_hostport]['zonefile_inv'] )
        peer_availability_ranking.append( (availability_score, peer_hostport) )
    
    if locked:
        atlas_peer_table_unlock()

    # sort on availability
    peer_availability_ranking.sort()
    peer_availability_ranking.reverse()

    return [peer_hp for _, peer_hp in peer_availability_ranking]


def atlas_peer_get_availability( local_inv, peer_inv ):
    """
    Calculate the number of zonefiles the given peer has that we don't have,
    given the local and peer availability inventory vectors
    """
    count = 0
    minlen = min( len(local_inv), len(peer_inv) )
    for i in xrange(0, minlen):
        local = ord(local_inv[i])
        remote = ord(peer_inv[i])
        for j in xrange(0, 8):
            if ((1 << j) & remote) != 0 and ((1 << j) & local) == 0:
                # this peer has this zonefile, but we don't
                count += 1

    if len(peer_inv) > len(local_inv):
        # this peer has more zonefiles
        for i in xrange(len(local_inv), len(peer_inv)):
            remote = ord(peer_inv[i])
            for j in xrange(0, 8):
                if ((1 << j) & remote) != 0:
                    # this peer has this zonefile, but we don't
                    count += 1
        
    return count


def atlas_peer_get_last_response_time( peer_hostport, peer_table=None ):
    """
    Get the last time we got a positive response
    from a peer.
    Return the time on success
    Return -1.0 if we never heard from them
    """
    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    last_time = -1.0
    if peer_hostport in peer_table.keys():
        for (t, r) in peer_table[peer_hostport]['time']:
            if r and t > last_time:
                last_time = peer_table[peer_hostport]['time']
    
    if locked:
        atlas_peer_table_unlock()
    
    return last_time


def atlas_peer_enqueue( peer_hostport, peer_table=None, peer_queue=None ):
    """
    Begin talking to a new peer, if we aren't already.
    Return the new peer queue.
    """

    peer_lock = False
    table_lock = False

    if peer_queue is None:
        peer_lock = True
        peer_queue = atlas_peer_queue_lock()

    if peer_table is None:
        table_lock = True
        peer_table = altas_peer_table_lock()

    present = (peer_hostport in peer_table.keys())

    if table_lock:
        atlas_peer_table_unlock()

    if not present:
        if len(peer_queue) < atlas_max_neighbors():
            peer_queue.append( peer_hostport )

    if peer_lock:
        atlas_peer_queue_unlock()

    return peer_queue


def atlas_peer_dequeue_all( peer_queue=None ):
    """
    Get all queued peers
    """
    peer_lock = False

    if peer_queue is None:
        peer_lock = True
        peer_queue = atlas_peer_queue_lock()

    peers = []
    while len(peer_queue) > 0:
        peers.append( peer_queue.pop(0) )

    if peer_lock:
        atlas_peer_queue_unlock()

    return peers


def atlas_zonefile_find_push_peers( zonefile_hash, peer_table=None, zonefile_bits=None, con=None, path=None ):
    """
    Find the set of peers that do *not* have this zonefile.
    """

    if zonefile_bits is None:
        zonefile_bits = atlasdb_get_zonefile_bits( zonefile_hash, path=path, con=con )
        if len(zonefile_bits) == 0:
            # we don't even know about it
            return []

    table_locked = False
    if peer_table is None:
        table_locked = True
        peer_table = atlas_peer_table_lock()

    push_peers = []
    for peer_hostport in peer_table.keys():
        zonefile_inv = peer_table[peer_hostport]['zonefile_inv']
        res = atlas_inventory_test_zonefile_bits( zonefile_inv, zonefile_bits )
        if res:
            push_peers.append( peer_hostport )

    if table_locked:
        atlas_peer_table_unlock()
        peer_table = None

    return push_peers


def atlas_zonefile_push_enqueue( zonefile_hash, zonefile_data, peer_table=None, zonefile_queue=None, con=None, path=None ):
    """
    Enqueue the given zonefile into our "push" queue.
    Only enqueue if we know of one peer that doesn't
    have it in its inventory vector.

    Return True if we enqueued it
    Return False if not
    """
    table_locked = False
    zonefile_queue_locked = False
    res = False

    bits = atlasdb_get_zonefile_bits( zonefile_hash, path=path, con=con )
    if len(bits) == 0:
        return

    if peer_table is None:
        table_locked = True
        peer_table = atlas_peer_table_lock()

    push_peers = atlas_zonefile_find_push_peers( zonefile_hash, peer_table=peer_table, zonefile_bits=bits )
    if len(push_peers) > 0:

        # someone needs this
        if zonefile_queue is None:
            zonefile_queue_locked = True
            zonefile_queue = atlas_zonefile_queue_lock()

        if len(zonefile_queue) < MAX_QUEUED_ZONEFILES: 
            zonefile_queue.append( {zonefile_hash: zonefile_data} )
            res = True
        
        if zonefile_queue_locked:
            atlas_zonefile_queue_unlock()

    return res


def atlas_zonefile_push_dequeue( zonefile_queue=None ):
    """
    Dequeue a zonefile to replicate
    """
    zonefile_queue_locked = False
    if zonefile_queue is None:
        zonefile_queue = atlas_zonefile_queue_lock()
        zonefile_queue_locked = True

    ret = None
    if len(zonefile_queue) > 0:
        ret = zonefile_queue.pop(0)

    if zonefile_queue_locked:
        atlas_zonefile_queue_unlock()

    return ret


class AtlasPeerCrawler( threading.Thread ):
    """
    Thread that continuously crawls peers.

    Try to obtain knowledge of as many peers as we can.
    (but we will only report the rarest NUM_NEIGHBORS peers to anyone who asks).
    We'll prune the set of known peers in another thread.

    The peer-selection algorithm works as follows:
    * search for neighbors via random walk: ask peers about their peers, and randomly expand them.
    * remember node popularity--how often we see that a peer knows about another peer, so we can report rare peers to other requesters
    """
    def __init__(self, my_hostname, my_portnum):
        threading.Thread.__init__(self)
        self.running = False
        self.my_hostport = "%s:%s" % (my_hostname, my_portnum)
        self.peer_crawl_list = []


    def step( self, lastblock, peer_table=None, peer_queue=None ):
        """
        Execute one round of the peer discovery algorithm:
        select a peer at random, get its neighbors,
        and remember to begin crawling them.
        """

        peer_queue = atlas_peer_dequeue_all( peer_queue=peer_queue )
        random.shuffle( peer_queue )

        # add new peers, if there's space
        for peer in peer_queue:
            if peer not in self.peer_crawl_list and len(self.peer_crawl_list) < 2 * NUM_NEIGHBORS:
                self.peer_crawl_list.append( peer )

        # talk to peers at random
        peer_hostport = self.peer_crawl_list[ random.randint(0, len(self.peer_crawl_list)+1) ]
        if peer_hostport == my_hostport:
            return
  
        log.debug("Crawl peer %s" % peer_hostport)

        # ask this peer for its K-rarest neighbors
        peers = atlas_peer_get_neighbors(self.my_hostport, peer_hostport, timeout=10 )

        if peers is None:
            log.debug("No peers from %s" % peer_hostport)
            return

        # Update peer table
        locked = False
        if peer_table is None:
            locked = True
            peer_table = atlas_peer_table_lock()

        # remove peers that have mostly the same data as us
        max_neighbors = atlas_max_neighbors()
        if len(peer_table.keys()) > max_neighbors:

            peers = atlas_rank_peers_by_availability( lastblock=lastblock, peer_table=peer_table)[:max_neighbors]
            atlas_remove_peers( peers, peer_table )

        # add new peers and update popularity
        for newpeer in peers:

            newhost, newport = url_to_host_port( newpeer )
            new_hostport = "%s:%s" % (host, port)
            
            atlas_peer_add_neighbor( peer_hostport, new_hostport, peer_table=peer_table )

            if new_hostport in peer_crawl_list:
                continue

            # did we know about this peer?
            if not peer_table.has_key(new_hostport):
                log.debug("New peer %s" % new_hostport)
                self.peer_crawl_list.append( new_hostport )

            # prune if we get too big
            if len(self.peer_crawl_list) > 2*max_neighbors:
                # remove one at random
                self.peer_crawl_list.pop( random.randint(0, len(self.peer_crawl_list)+1) )


        if locked:
            atlas_peer_table_unlock()
            peer_table = None


    def run(self):
        self.running = True

        # initial crawl list
        global_peer_table = atlas_peer_table_lock()
        self.peer_crawl_list = global_peer_table.keys()[:]
        atlas_peer_table_unlock()

        while self.running:
            self.step()


    def ask_join(self):
        self.running = False


class AtlasHealthChecker( threading.Thread ):
    """
    Thread that continuously monitors the health
    of our neighbor set.  It will remove unhealthy
    peers if we have too many neighbors (over NUM_NEIGHBORS).

    Unhealthy is defined as "we can't reach them, or
    they don't have zonefiles that we need"
    """
    def __init__(self, my_host, my_port, path=None):
        threading.Thread.__init__(self)
        self.running = False
        self.path = path
        self.hostport = "%s:%s" % (my_host, my_port)
        if path is None:
            path = atlasdb_path()


    def step(self, con=None, path=None, peer_table=None):
        """
        Run one step of this algorithm.
        Ping the given neighbor for its zonefile inventory,
        and update our local copy (starting from startblock).

        Return True on success
        Return False on error
        """
        if path is None:
            path = self.path

        lock = False
        if peer_table is None:
            lock = True
            peer_table = peer_table_lock()
        
        # find someone to ping
        num_peers = len(peer_table.keys())
        peers = peer_table.keys()[:]

        random.shuffle( peers )

        # who are we going to ping?
        # someone we haven't pinged in a while, chosen at random
        peer_hostport = None
        for peer in peers:
            if not atlas_peer_has_fresh_zonefile_inventory( peer, peer_table=peer_table ):
                # haven't talked to this peer in a while
                peer_hostport = peer
                break

        if lock:
            peer_table_unlock()
            peer_table = None

        if peer_hostport:
            # have someone to ping
            # TODO: startblock should be the smallest block number of the current zonefile set
            log.debug("Refresh zonefile inventory for %s" % peer_hostport)
            res = atlas_peer_refresh_zonefile_inventory( peer_hostport, FIRST_BLOCK_MAINNET, con=con, path=path, peer_table=peer_table )
            if res is None:
                log.warning("Failed to refresh zonefile inventory for %s" % peer_hostport)

        else:
            time_sleep(self.hostport, self.__class.__name__, 1.0)

        return 


    def run(self, peer_table=None):
        """
        Loop forever, pinging someone every pass.
        """
        while self.running:
            # TODO: first block should be the smallest block in the *current zonefile set*
            # that we don't have a zonefile for, or lastblock - 5000 (whichever is smaller)
            self.step( peer_table=peer_table )


    def ask_join(self):
        self.running = False


class AtlasZonefileCrawler( threading.Thread ):
    """
    Thread that continuously tries to find 
    zonefiles that we don't have.
    """

    def __init__(self, my_host, my_port, zonefile_storage_drivers=[], path=None, zonefile_dir=None):
        threading.Thread.__init__(self)
        self.running = False
        self.hostport = "%s:%s" % (my_host, my_port)
        self.path = path 
        self.zonefile_storage_drivers = zonefile_storage_drivers
        self.zonefile_dir = zonefile_dir
        if self.path is None:
            self.path = atlasdb_path()

    
    def step(self, con=None, path=None, peer_table=None):
        """
        Run one step of this algorithm:
        * find the set of missing zonefiles
        * try to fetch each of them
        * store them
        * update our zonefile database

        Fetch rarest zonefiles first, but batch
        whenever possible.

        Return the number of zonefiles fetched
        """

        if path is None:
            path = self.path

        close = False
        if con is None:
            close = True
            con = atlasdb_open( path )

        num_fetched = 0
        locked = False

        if peer_table is None:
            locked = True
            peer_table = atlas_peer_table_lock()

        missing_zfinfo = atlas_find_missing_zonefile_availability( peer_table=peer_table, con=con, path=path )

        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        # ask in rarest-first order
        zonefile_ranking = [ (missing_zfinfo[zfhash]['popularity'], zfhash) for zfhash in missing_zfinfo.keys() ]
        zonefile_ranking.sort()
        zonefile_hashes = list(set([zfhash for (_, zfhash) in zonefile_ranking]))

        zonefile_origins = {}   # map peer hostport to list of zonefile hashes

        # which peers can serve each zonefile?
        for zfhash in missing_zfinfo.keys():
            if not zonefile_origins.has_key(peer_hostport):
                zonefile_origins[peer_hostport] = []

            zonefile_origins[peer_hostport].append( zfhash )

        while len(zonefile_hashes) > 0:
            zfhash = zonefile_hashes[0]
            peers = missing_zfinfo[zfhash]['peers']

            # try this zonefile's hosts in order by perceived availability
            peers = atlas_rank_peers_by_health( peer_list=peers )
            for peer_hostport in peers:

                # what other zonefiles can we get?
                # only ask for the ones we don't have
                peer_zonefile_hashes = zonefile_origins[peer_hostport]
                already_have = []
                for zfh in peer_zonefile_hashes:
                    if zfh not in zonefile_hashes:
                        already_have.append(zfh)

                for zfh in already_have:
                    peer_zonefile_hashes.remove(zfh)

                if len(peer_zonefile_hashes) == 0:
                    continue

                # get them all
                zonefiles = atlas_get_zonefiles( peer_hostport, peer_zonefile_hashes, peer_table=peer_table )
                if zonefiles is not None:
                    # got zonefiles!
                    for fetched_zfhash in zonefiles.keys():
                        if fetched_zfhash not in peer_zonefile_hashes:
                            # unsolicited
                            log.warn("Unsolicited zonefile %s" % fetched_zfhash)
                            continue

                        rc = store_zonefile_to_storage( zonefiles[fetched_zfhash], required=self.zonefile_storage_drivers, cache=True, zonefile_dir=zonefile_dir )
                        if not rc:
                            log.error("Failed to store zonefile %s" % fetched_zfhash)

                        else:
                            # stored! remember it
                            atlasdb_set_zonefile_present( fetched_zfhash, True, con=con, path=path )
                            zonefile_hashes.remove(fetched_zfhash)
                            peer_zonefile_hashes.remove(fetched_zfhash)
                            num_fetched += 1

                if locked:
                    peer_table = atlas_peer_table_lock()

                # if the node didn't actually have these zonefiles, then 
                # update their inventories so we don't ask for them again.
                for zfhash in peer_zonefile_hashes:
                    atlas_peer_set_zonefile_status( peer_hostport, zfhash, False, zonefile_bits=missing_zfinfo[zfhash]['indexes'], peer_table=peer_table )

                if locked:
                    atlas_peer_table_unlock()
                    peer_table = None

            zonefile_hashes.pop(0)

        if close:
            con.close()

        return num_fetched

    
    def run(self):
        self.running = True
        while self.running:
            con = atlasdb_open( self.path )
            num_fetched = self.step( con=con, path=self.path )
            con.close()
            
            if num_fetched == 0:
                time_sleep(self.hostport, self.__class__.__name__, 1.0)


class AtlasZonefilePusher(object):
    """
    Continuously drain the queue of zonefiles
    we can push, by sending them off to 
    known peers who need them.
    """
    def __init__(self, host, port ):
        self.host = host
        self.port = port
        self.hostport = "%s:%s" % (host, port)


    def step( self, peer_table=None, zonefile_queue=None ):
        """
        Run one step of this algorithm.
        Push the zonefile to all the peers that need it.
        Return the number of peers we sent to
        """
        zfinfo = atlas_zonefile_push_dequeue( zonefile_queue=zonefile_queue )

        zfbits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )
        if len(zfbits) == 0:
            # nope 
            return 0

        zfhash = zfinfo.keys()[0]
        zfdata = zfinfo[zfhash]

        # see if we can send this somewhere
        table_locked = False
        if peer_table is None:
            peer_table = atlas_peer_table_lock()
            table_locked = True

        peers = atlas_zonefile_find_push_peers( zfhash, peer_table=peer_table, zonefile_bits=zfbits )

        if table_locked:
            atlas_peer_table_unlock()
            peer_table = None

        if len(peers) == 0:
            # everyone has it
            return 0

        # push it off
        ret = 0
        for peer in peers:
            atlas_zonefile_push( peer, zfhash, zfdata, timeout=10 )
            ret += 1

        return ret

    
    def run(self):
        self.running = True
        while self.running:
            num_pushed = self.step()
            if num_pushed == 0:
                time_sleep(self.hostport, self.__class__.__name__, 1.0)
        
