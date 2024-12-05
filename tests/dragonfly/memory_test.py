import pytest
from redis import asyncio as aioredis
from .utility import *
import logging
from . import dfly_args
from .instance import DflyInstance, DflyInstanceFactory


async def calculate_estimated_connection_memory(
    async_client: aioredis.Redis, df_server: DflyInstance
):
    memory_info = await async_client.info("memory")
    already_used_rss_memory = memory_info["used_memory_rss"]

    connections_number = 100
    connections = []
    for _ in range(connections_number):
        conn = aioredis.Redis(port=df_server.port)
        await conn.ping()
        connections.append(conn)

    await asyncio.sleep(1)  # Wait RSS update

    memory_info = await async_client.info("memory")
    estimated_connections_memory = memory_info["used_memory_rss"] - already_used_rss_memory

    # Close test connection
    for conn in connections:
        await conn.close()

    return estimated_connections_memory // connections_number


@pytest.mark.opt_only
@pytest.mark.parametrize(
    "type, keys, val_size, elements",
    [
        ("JSON", 200_000, 100, 100),
        ("SET", 280_000, 100, 100),
        ("HASH", 250_000, 100, 100),
        ("ZSET", 250_000, 100, 100),
        ("LIST", 300_000, 100, 100),
        ("STRING", 3_500_000, 1000, 1),
        ("STREAM", 260_000, 100, 100),
    ],
)
# We limit to 5gb just in case to sanity check the gh runner. Otherwise, if we ask for too much
# memory it might force the gh runner to run out of memory (since OOM killer might not even
# get a chance to run).
@dfly_args({"proactor_threads": 4, "maxmemory": "5gb"})
async def test_rss_used_mem_gap(df_server: DflyInstance, type, keys, val_size, elements):
    # Create a Dragonfly and fill it up with `type` until it reaches `min_rss`, then make sure that
    # the gap between used_memory and rss is no more than `max_unaccounted_ratio`.
    min_rss = 3 * 1024 * 1024 * 1024  # 3gb
    max_unaccounted = 200 * 1024 * 1024  # 200mb

    # There is a big rss spike when this test is ran in one the gh runners (not the self hosted)
    # and it fails. This rss spike is not observed locally or on our self host runner so
    # this adjustment is mostly for CI
    if type == "STREAM":
        max_unaccounted = max_unaccounted * 3

    client = df_server.client()
    await asyncio.sleep(1)  # Wait for another RSS heartbeat update in Dragonfly

    cmd = f"DEBUG POPULATE {keys} {type} {val_size} RAND TYPE {type} ELEMENTS {elements}"
    print(f"Running {cmd}")
    await client.execute_command(cmd)

    await asyncio.sleep(2)  # Wait for another RSS heartbeat update in Dragonfly

    info = await client.info("memory")
    logging.info(f'Used memory {info["used_memory"]}, rss {info["used_memory_rss"]}')
    assert info["used_memory"] > min_rss, "Weak testcase: too little used memory"
    delta = info["used_memory_rss"] - info["used_memory"]
    # It could be the case that the machine is configured to use swap if this assertion fails
    assert delta > 0
    assert delta < max_unaccounted
    delta = info["used_memory_rss"] - info["object_used_memory"]
    # TODO investigate why it fails on string
    if type == "JSON" or type == "STREAM":
        assert delta > 0
        assert delta < max_unaccounted


@pytest.mark.asyncio
@dfly_args(
    {
        "maxmemory": "512mb",
        "proactor_threads": 2,
        "rss_oom_deny_ratio": 0.5,
    }
)
@pytest.mark.parametrize("admin_port", [0, 1112])
async def test_rss_oom_ratio(df_factory: DflyInstanceFactory, admin_port):
    """
    Test dragonfly rejects denyoom commands and new connections when rss memory is above maxmemory*rss_oom_deny_ratio
    Test dragonfly does not rejects when rss memory goes below threshold
    """
    df_server = df_factory.create(admin_port=admin_port)
    df_server.start()

    client = df_server.client()
    await client.execute_command("DEBUG POPULATE 10000 key 40000 RAND")

    await asyncio.sleep(1)  # Wait for another RSS heartbeat update in Dragonfly

    new_client = df_server.admin_client() if admin_port else df_server.client()
    await new_client.ping()

    info = await new_client.info("memory")
    logging.debug(f'Used memory {info["used_memory"]}, rss {info["used_memory_rss"]}')

    reject_limit = 256 * 1024 * 1024  # 256mb
    assert info["used_memory_rss"] > reject_limit

    # get command from existing connection should not be rejected
    await client.execute_command("get x")

    # reject set due to oom
    with pytest.raises(redis.exceptions.ResponseError):
        await client.execute_command("set x y")

    if admin_port:
        # new client create should also fail if admin port was set
        client = df_server.client()
        with pytest.raises(redis.exceptions.ConnectionError):
            await client.ping()

    # flush to free memory
    await new_client.flushall()

    await asyncio.sleep(2)  # Wait for another RSS heartbeat update in Dragonfly

    info = await new_client.info("memory")
    logging.debug(f'Used memory {info["used_memory"]}, rss {info["used_memory_rss"]}')
    assert info["used_memory_rss"] < reject_limit

    # new client create shoud not fail after memory usage decrease
    client = df_server.client()
    await client.execute_command("set x y")


@pytest.mark.asyncio
@dfly_args(
    {
        "maxmemory": "512mb",
        "proactor_threads": 1,
    }
)
async def test_eval_with_oom(df_factory: DflyInstanceFactory):
    """
    Test running eval commands when dragonfly returns OOM on write commands and check rss memory
    This test was writen after detecting memory leak in script runs on OOM state
    """
    df_server = df_factory.create()
    df_server.start()

    client = df_server.client()
    await client.execute_command("DEBUG POPULATE 20000 key 40000 RAND")

    await asyncio.sleep(1)  # Wait for another RSS heartbeat update in Dragonfly

    info = await client.info("memory")
    logging.debug(f'Used memory {info["used_memory"]}, rss {info["used_memory_rss"]}')

    reject_limit = 512 * 1024 * 1024  # 256mb
    assert info["used_memory"] > reject_limit
    rss_before_eval = info["used_memory_rss"]

    pipe = client.pipeline(transaction=False)
    MSET_SCRIPT = """
        redis.call('MSET', KEYS[1], ARGV[1], KEYS[2], ARGV[2])
    """

    for _ in range(20):
        for _ in range(8000):
            pipe.eval(MSET_SCRIPT, 2, "x1", "y1", "x2", "y2")
        # reject mset due to oom
        with pytest.raises(redis.exceptions.ResponseError):
            await pipe.execute()

    await asyncio.sleep(1)  # Wait for another RSS heartbeat update in Dragonfly

    info = await client.info("memory")
    logging.debug(f'Used memory {info["used_memory"]}, rss {info["used_memory_rss"]}')
    assert rss_before_eval * 1.01 > info["used_memory_rss"]


@pytest.mark.asyncio
@dfly_args(
    {
        "proactor_threads": 1,
        "cache_mode": "true",
        "maxmemory": "256mb",
        "rss_oom_deny_ratio": 0.5,
        "max_eviction_per_heartbeat": 1000,
    }
)
async def test_cache_eviction_with_rss_deny_oom(
    async_client: aioredis.Redis,
    df_server: DflyInstance,
):
    """
    Test to verify that cache eviction is triggered even if used memory is small but rss memory is above limit
    """

    max_memory = 256 * 1024 * 1024  # 256 MB
    rss_max_memory = int(max_memory * 0.5)  # 50% of max memory

    data_fill_size = int(0.55 * rss_max_memory)  # 55% of rss_max_memory
    rss_increase_size = int(0.55 * rss_max_memory)  # 55% of max rss_max_memory

    key_size = 1024 * 5  # 5 kb
    num_keys = data_fill_size // key_size

    await asyncio.sleep(1)  # Wait for RSS update

    estimated_connection_memory = await calculate_estimated_connection_memory(
        async_client, df_server
    )
    num_connections = rss_increase_size // estimated_connection_memory

    logging.info(
        f"Estimated connection memory: {estimated_connection_memory}. Number of connections: {num_connections}."
    )

    # Fill data to 55% of rss max memory
    await async_client.execute_command("DEBUG", "POPULATE", num_keys, "key", key_size)

    await asyncio.sleep(1)  # Wait for RSS heartbeat update

    # First test that eviction is not triggered without connection creation
    stats_info = await async_client.info("stats")
    assert stats_info["evicted_keys"] == 0, "No eviction should start yet."

    # Test that used memory is less than 90% of max memory
    memory_info = await async_client.info("memory")
    assert (
        memory_info["used_memory"] < max_memory * 0.9
    ), "Used memory should be less than 90% of max memory."
    assert (
        memory_info["used_memory_rss"] < rss_max_memory * 0.9
    ), "RSS memory should be less than 90% of rss max memory (max_memory * rss_oom_deny_ratio)."

    # Disable heartbeat eviction
    await async_client.execute_command("CONFIG SET enable_heartbeat_eviction false")

    # Increase RSS memory by 55% of rss max memory
    # We can simulate RSS increase by creating new connections
    connections = []
    for _ in range(num_connections):
        conn = aioredis.Redis(port=df_server.port)
        await conn.ping()
        connections.append(conn)

    await asyncio.sleep(1)

    # Check that RSS memory is above rss limit
    memory_info = await async_client.info("memory")
    assert (
        memory_info["used_memory_rss"] >= rss_max_memory * 0.9
    ), "RSS memory should exceed 90% of the maximum RSS memory limit (max_memory * rss_oom_deny_ratio)."

    # Enable heartbeat eviction
    await async_client.execute_command("CONFIG SET enable_heartbeat_eviction true")

    await asyncio.sleep(1)  # Wait for RSS heartbeat update
    await async_client.execute_command("MEMORY DECOMMIT")
    await asyncio.sleep(1)  # Wait for RSS update

    # Get RSS memory after creating new connections
    memory_info = await async_client.info("memory")
    stats_info = await async_client.info("stats")

    logging.info(f'Evicted keys number: {stats_info["evicted_keys"]}. Total keys: {num_keys}.')

    assert (
        memory_info["used_memory"] < data_fill_size
    ), "Used memory should be less than initial fill size due to eviction."

    assert (
        memory_info["used_memory_rss"] < rss_max_memory * 0.9
    ), "RSS memory should be less than 90% of rss max memory (max_memory * rss_oom_deny_ratio) after eviction."

    # Check that eviction has occurred
    assert (
        stats_info["evicted_keys"] > 0
    ), "Eviction should have occurred due to rss memory pressure."

    for conn in connections:
        await conn.close()
