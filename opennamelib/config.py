# Global magic bytes
MAGIC_BYTES_MAINSPACE = 'X\x08'
MAGIC_BYTES_TESTSPACE = 'X\x88'

# Opcodes
NAME_PREORDER = 'a'
NAME_REGISTRATION = 'b'
NAME_UPDATE = 'c'
NAME_TRANSFER = 'd'
NAME_RENEWAL = 'e'

# Other
LENGTHS = {
    'magic_bytes': 2,
    'opcode': 1,
    'name_hash': 20,
    'consensus_hash': 16,
    'name_min': 1,
    'name_max': 16,
    'unencoded_name': 24,
    'salt': 16,
    'update_hash': 20,
}

MIN_OP_LENGTHS = {
    'preorder': LENGTHS['name_hash'],
    'registration': LENGTHS['name_min'] + LENGTHS['salt'],
    'update': LENGTHS['name_min'] + LENGTHS['update_hash'],
    'transfer': LENGTHS['name_min']
}

OP_RETURN_MAX_SIZE = 40

FIRST_BLOCK_MAINNET = 334750
FIRST_BLOCK_MAINNET_TESTSPACE = 334750
FIRST_BLOCK_TESTNET = 311517
FIRST_BLOCK_TESTNET_TESTSPACE = 311517

DEFAULT_OP_RETURN_FEE = 10000
DEFAULT_DUST_SIZE = 5500
DEFAULT_OP_RETURN_VALUE = 0
DEFAULT_FEE_PER_KB = 10000

AVERAGE_MINUTES_PER_BLOCK = 10
DAYS_PER_YEAR, HOURS_PER_DAY, MINUTES_PER_HOUR = 365.2424, 24, 60
MINUTES_PER_YEAR = DAYS_PER_YEAR*HOURS_PER_DAY*MINUTES_PER_HOUR
BLOCKS_PER_YEAR = int(round(MINUTES_PER_YEAR/AVERAGE_MINUTES_PER_BLOCK))
EXPIRATION_PERIOD = BLOCKS_PER_YEAR*1 #EXPIRATION_PERIOD = 10
AVERAGE_BLOCKS_PER_HOUR = MINUTES_PER_HOUR/AVERAGE_MINUTES_PER_BLOCK

SATOSHIS_PER_BTC = 10**8
PRICE_FOR_1LETTER_NAMES = 10*SATOSHIS_PER_BTC
PRICE_DROP_PER_LETTER = 10
PRICE_DROP_FOR_NON_ALPHABETIC = 10
ALPHABETIC_PRICE_FLOOR = 10**4

BLOCKS_CONSENSUS_HASH_IS_VALID = 4*AVERAGE_BLOCKS_PER_HOUR