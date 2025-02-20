import inspect
import random
import threading
import time
from multiprocessing.dummy import Pool
from helpers.test_tools import assert_logs_contain_with_retry

import pytest
from helpers.client import QueryRuntimeException
from helpers.cluster import ClickHouseCluster
from helpers.network import PartitionManager
from helpers.test_tools import assert_eq_with_retry

# FIXME: each sleep(1) is a time bomb, and not only this cause false positive
# it also makes the test not reliable (i.e. assertions may be wrong, due timing issues)
# Seems that some SYSTEM query should be added to wait those things insteadof sleep.

cluster = ClickHouseCluster(__file__)

node1 = cluster.add_instance(
    "node1",
    main_configs=[
        "configs/logs_config.xml",
        "configs/config.d/instant_moves.xml",
        "configs/config.d/storage_configuration.xml",
        "configs/config.d/cluster.xml",
    ],
    with_zookeeper=True,
    tmpfs=["/jbod1:size=40M", "/jbod2:size=40M", "/external:size=200M"],
    macros={"shard": 0, "replica": 1},
    stay_alive=True,
)

node2 = cluster.add_instance(
    "node2",
    main_configs=[
        "configs/logs_config.xml",
        "configs/config.d/instant_moves.xml",
        "configs/config.d/storage_configuration.xml",
        "configs/config.d/cluster.xml",
    ],
    with_zookeeper=True,
    tmpfs=["/jbod1:size=40M", "/jbod2:size=40M", "/external:size=200M"],
    macros={"shard": 0, "replica": 2},
)


@pytest.fixture(scope="module")
def started_cluster():
    try:
        cluster.start()
        yield cluster

    finally:
        cluster.shutdown()


def get_used_disks_for_table(node, table_name, partition=None):
    if partition is None:
        suffix = ""
    else:
        suffix = "and partition='{}'".format(partition)
    return (
        node.query(
            """
        SELECT disk_name
        FROM system.parts
        WHERE table == '{name}' AND active=1 {suffix}
        ORDER BY modification_time
    """.format(
                name=table_name, suffix=suffix
            )
        )
        .strip()
        .split("\n")
    )


def check_used_disks_with_retry(node, table_name, expected_disks, retries):
    for _ in range(retries):
        used_disks = get_used_disks_for_table(node, table_name)
        if set(used_disks).issubset(expected_disks):
            return True
        time.sleep(0.5)
    return False


# Use unique table name for flaky checker, that run tests multiple times
def unique_table_name(base_name):
    return f"{base_name}_{int(time.time())}"


def wait_parts_mover(node, table, *args, **kwargs):
    # wait for MergeTreePartsMover
    assert_logs_contain_with_retry(
        node, f"default.{table}.*Removed part from old location", *args, **kwargs
    )


@pytest.mark.parametrize(
    "name,engine,alter",
    [
        pytest.param(
            "mt_test_rule_with_invalid_destination", "MergeTree()", 0, id="case0"
        ),
        pytest.param(
            "replicated_mt_test_rule_with_invalid_destination",
            "ReplicatedMergeTree('/clickhouse/replicated_test_rule_with_invalid_destination', '1')",
            0,
            id="case1",
        ),
        pytest.param(
            "mt_test_rule_with_invalid_destination", "MergeTree()", 1, id="case2"
        ),
        pytest.param(
            "replicated_mt_test_rule_with_invalid_destination",
            "ReplicatedMergeTree('/clickhouse/replicated_test_rule_with_invalid_destination', '1')",
            1,
            id="case3",
        ),
    ],
)
def test_rule_with_invalid_destination(started_cluster, name, engine, alter):
    name = unique_table_name(name)

    try:

        def get_command(x, policy):
            x = x or ""
            if alter and x:
                return """
                    ALTER TABLE {name} MODIFY TTL {expression}
                """.format(
                    expression=x, name=name
                )
            else:
                return """
                    CREATE TABLE {name} (
                        s1 String,
                        d1 DateTime
                    ) ENGINE = {engine}
                    ORDER BY tuple()
                    {expression}
                    SETTINGS storage_policy='{policy}'
                """.format(
                    expression=x, name=name, engine=engine, policy=policy
                )

        if alter:
            node1.query(get_command(None, "small_jbod_with_external"))

        with pytest.raises(QueryRuntimeException):
            node1.query(
                get_command("TTL d1 TO DISK 'unknown'", "small_jbod_with_external")
            )

        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))

        if alter:
            node1.query(get_command(None, "small_jbod_with_external"))

        with pytest.raises(QueryRuntimeException):
            node1.query(
                get_command("TTL d1 TO VOLUME 'unknown'", "small_jbod_with_external")
            )

        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))

        if alter:
            node1.query(get_command(None, "only_jbod2"))

        with pytest.raises(QueryRuntimeException):
            node1.query(get_command("TTL d1 TO DISK 'jbod1'", "only_jbod2"))

        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))

        if alter:
            node1.query(get_command(None, "only_jbod2"))

        with pytest.raises(QueryRuntimeException):
            node1.query(get_command("TTL d1 TO VOLUME 'external'", "only_jbod2"))

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine,positive",
    [
        pytest.param(
            "mt_test_inserts_to_disk_do_not_work",
            "MergeTree()",
            0,
            id="mt_test_inserts_to_disk_do_not_work",
        ),
        pytest.param(
            "replicated_mt_test_inserts_to_disk_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_inserts_to_disk_do_not_work', '1')",
            0,
            id="replicated_mt_test_inserts_to_disk_do_not_work",
        ),
        pytest.param(
            "mt_test_inserts_to_disk_work",
            "MergeTree()",
            1,
            id="mt_test_inserts_to_disk_work_1",
        ),
        pytest.param(
            "replicated_mt_test_inserts_to_disk_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_inserts_to_disk_work', '1')",
            1,
            id="replicated_mt_test_inserts_to_disk_work_1",
        ),
    ],
)
def test_inserts_to_disk_work(started_cluster, name, engine, positive):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO DISK 'external'
            SETTINGS storage_policy='small_jbod_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        data = []  # 10MB in total
        for i in range(10):
            data.append(
                (
                    "randomPrintableASCII(1024*1024)",
                    "toDateTime({})".format(
                        time.time() - 1 if i > 0 or positive else time.time() + 300
                    ),
                )
            )

        node1.query(
            "INSERT INTO {} (s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )
        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external" if positive else "jbod1"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        try:
            node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))
        except:
            pass


@pytest.mark.parametrize(
    "name,engine",
    [
        pytest.param(
            "mt_test_moves_work_after_storage_policy_change",
            "MergeTree()",
            id="mt_test_moves_work_after_storage_policy_change",
        ),
        pytest.param(
            "replicated_mt_test_moves_work_after_storage_policy_change",
            "ReplicatedMergeTree('/clickhouse/test_moves_work_after_storage_policy_change', '1')",
            id="replicated_mt_test_moves_work_after_storage_policy_change",
        ),
    ],
)
def test_moves_work_after_storage_policy_change(started_cluster, name, engine):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
        """.format(
                name=name, engine=engine
            )
        )

        node1.query(
            """ALTER TABLE {name} MODIFY SETTING storage_policy='default_with_small_jbod_with_external'""".format(
                name=name
            )
        )

        # Second expression is preferred because d1 > now()-3600.
        node1.query(
            """ALTER TABLE {name} MODIFY TTL now()-3600 TO DISK 'jbod1', d1 TO DISK 'external'""".format(
                name=name
            )
        )

        wait_expire_1 = 12
        wait_expire_2 = 4
        time_1 = time.time() + wait_expire_1

        data = []  # 10MB in total
        for i in range(10):
            data.append(
                ("randomPrintableASCII(1024*1024)", "toDateTime({})".format(time_1))
            )

        node1.query(
            "INSERT INTO {} (s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )
        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}

        wait_parts_mover(node1, name, retry_count=40)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine,positive",
    [
        pytest.param(
            "mt_test_moves_to_disk_do_not_work",
            "MergeTree()",
            0,
            id="mt_test_moves_to_disk_do_not_work",
        ),
        pytest.param(
            "replicated_mt_test_moves_to_disk_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_to_disk_do_not_work', '1')",
            0,
            id="replicated_mt_test_moves_to_disk_do_not_work",
        ),
        pytest.param(
            "mt_test_moves_to_disk_work",
            "MergeTree()",
            1,
            id="mt_test_moves_to_disk_work",
        ),
        pytest.param(
            "replicated_mt_test_moves_to_disk_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_to_disk_work', '1')",
            1,
            id="replicated_mt_test_moves_to_disk_work",
        ),
    ],
)
def test_moves_to_disk_work(started_cluster, name, engine, positive):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO DISK 'external'
            SETTINGS storage_policy='small_jbod_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        wait_expire_1 = 12
        wait_expire_2 = 20
        time_1 = time.time() + wait_expire_1
        time_2 = time.time() + wait_expire_1 + wait_expire_2

        wait_expire_1_thread = threading.Thread(
            target=time.sleep, args=(wait_expire_1,)
        )
        wait_expire_1_thread.start()

        data = []  # 10MB in total
        for i in range(10):
            data.append(
                (
                    "randomPrintableASCII(1024*1024)",
                    "toDateTime({})".format(time_1 if i > 0 or positive else time_2),
                )
            )

        node1.query(
            "INSERT INTO {} (s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )
        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}

        wait_expire_1_thread.join()
        time.sleep(wait_expire_2 / 2)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external" if positive else "jbod1"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine",
    [
        pytest.param(
            "mt_test_moves_to_volume_work",
            "MergeTree()",
            id="mt_test_moves_to_volume_work",
        ),
        pytest.param(
            "replicated_mt_test_moves_to_volume_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_to_volume_work', '1')",
            id="replicated_mt_test_moves_to_volume_work",
        ),
    ],
)
def test_moves_to_volume_work(started_cluster, name, engine):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                p1 Int64,
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            PARTITION BY p1
            TTL d1 TO VOLUME 'external'
            SETTINGS storage_policy='jbods_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        wait_expire_1 = 10
        time_1 = time.time() + wait_expire_1

        for p in range(2):
            data = []  # 10MB in total
            for i in range(5):
                data.append(
                    (
                        str(p),
                        "randomPrintableASCII(1024*1024)",
                        "toDateTime({})".format(time_1),
                    )
                )

            node1.query(
                "INSERT INTO {} (p1, s1, d1) VALUES {}".format(
                    name, ",".join(["(" + ",".join(x) + ")" for x in data])
                )
            )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1", "jbod2"}

        wait_parts_mover(node1, name, retry_count=40)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine,positive",
    [
        pytest.param(
            "mt_test_inserts_to_volume_do_not_work",
            "MergeTree()",
            0,
            id="mt_test_inserts_to_volume_do_not_work",
        ),
        pytest.param(
            "replicated_mt_test_inserts_to_volume_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_inserts_to_volume_do_not_work', '1')",
            0,
            id="replicated_mt_test_inserts_to_volume_do_not_work",
        ),
        pytest.param(
            "mt_test_inserts_to_volume_work",
            "MergeTree()",
            1,
            id="mt_test_inserts_to_volume_work",
        ),
        pytest.param(
            "replicated_mt_test_inserts_to_volume_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_inserts_to_volume_work', '1')",
            1,
            id="replicated_mt_test_inserts_to_volume_work",
        ),
    ],
)
def test_inserts_to_volume_work(started_cluster, name, engine, positive):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                p1 Int64,
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            PARTITION BY p1
            TTL d1 TO VOLUME 'external'
            SETTINGS storage_policy='small_jbod_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        node1.query("SYSTEM STOP MOVES {name}".format(name=name))

        for p in range(2):
            data = []  # 20MB in total
            for i in range(10):
                data.append(
                    (
                        str(p),
                        "randomPrintableASCII(1024*1024)",
                        "toDateTime({})".format(
                            time.time() - 1 if i > 0 or positive else time.time() + 300
                        ),
                    )
                )

            node1.query(
                "INSERT INTO {} (p1, s1, d1) VALUES {}".format(
                    name, ",".join(["(" + ",".join(x) + ")" for x in data])
                )
            )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external" if positive else "jbod1"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "20"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine",
    [
        pytest.param(
            "mt_test_moves_to_disk_eventually_work",
            "MergeTree()",
            id="mt_test_moves_to_disk_eventually_work",
        ),
        pytest.param(
            "replicated_mt_test_moves_to_disk_eventually_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_to_disk_eventually_work', '1')",
            id="replicated_mt_test_moves_to_disk_eventually_work",
        ),
    ],
)
def test_moves_to_disk_eventually_work(started_cluster, name, engine):
    name = unique_table_name(name)

    try:
        name_temp = name + "_temp"

        node1.query(
            """
            CREATE TABLE {name} (
                s1 String
            ) ENGINE = MergeTree()
            ORDER BY tuple()
            SETTINGS storage_policy='only_jbod2'
        """.format(
                name=name_temp
            )
        )

        data = []  # 35MB in total
        for i in range(35):
            data.append("randomPrintableASCII(1024*1024)")

        node1.query(
            "INSERT INTO {} VALUES {}".format(
                name_temp, ",".join(["(" + x + ")" for x in data])
            )
        )
        used_disks = get_used_disks_for_table(node1, name_temp)
        assert set(used_disks) == {"jbod2"}

        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO DISK 'jbod2'
            SETTINGS storage_policy='jbod1_with_jbod2'
        """.format(
                name=name, engine=engine
            )
        )

        data = []  # 10MB in total
        for i in range(10):
            data.append(
                (
                    "randomPrintableASCII(1024*1024)",
                    "toDateTime({})".format(time.time() - 1),
                )
            )

        node1.query(
            "INSERT INTO {} (s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )
        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}

        node1.query("DROP TABLE {} SYNC".format(name_temp))

        wait_parts_mover(node1, name)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod2"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name_temp))
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


def test_replicated_download_ttl_info(started_cluster):
    name = unique_table_name("test_replicated_ttl_info")
    engine = "ReplicatedMergeTree('/clickhouse/test_replicated_download_ttl_info', '{replica}')"
    try:
        for i, node in enumerate((node1, node2), start=1):
            node.query(
                """
                CREATE TABLE {name} (
                    s1 String,
                    d1 DateTime
                ) ENGINE = {engine}
                ORDER BY tuple()
                TTL d1 TO DISK 'external'
                SETTINGS storage_policy='small_jbod_with_external'
            """.format(
                    name=name, engine=engine
                )
            )

        node1.query("SYSTEM STOP MOVES {}".format(name))

        node2.query(
            "INSERT INTO {} (s1, d1) VALUES (randomPrintableASCII(1024*1024), toDateTime({}))".format(
                name, time.time() - 100
            )
        )

        assert set(get_used_disks_for_table(node2, name)) == {"external"}

        time.sleep(1)

        assert node1.query("SELECT count() FROM {}".format(name)).splitlines() == ["1"]
        assert set(get_used_disks_for_table(node1, name)) == {"external"}

    finally:
        for node in (node1, node2):
            try:
                node.query("DROP TABLE IF EXISTS {} SYNC".format(name))
            except:
                continue


@pytest.mark.parametrize(
    "name,engine,positive",
    [
        pytest.param(
            "mt_test_merges_to_disk_do_not_work",
            "MergeTree()",
            0,
            id="mt_test_merges_to_disk_do_not_work",
        ),
        pytest.param(
            "replicated_mt_test_merges_to_disk_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_merges_to_disk_do_not_work', '1')",
            0,
            id="mt_test_merges_to_disk_do_not_work",
        ),
        pytest.param(
            "mt_test_merges_to_disk_work",
            "MergeTree()",
            1,
            id="mt_test_merges_to_disk_work",
        ),
        pytest.param(
            "replicated_mt_test_merges_to_disk_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_merges_to_disk_work', '1')",
            1,
            id="replicated_mt_test_merges_to_disk_work",
        ),
    ],
)
def test_merges_to_disk_work(started_cluster, name, engine, positive):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO DISK 'external'
            SETTINGS storage_policy='small_jbod_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        node1.query("SYSTEM STOP MERGES {}".format(name))
        node1.query("SYSTEM STOP MOVES {}".format(name))

        wait_expire_1 = 16
        wait_expire_2 = 20
        time_1 = time.time() + wait_expire_1
        time_2 = time.time() + wait_expire_1 + wait_expire_2

        wait_expire_1_thread = threading.Thread(
            target=time.sleep, args=(wait_expire_1,)
        )
        wait_expire_1_thread.start()

        for _ in range(2):
            data = []  # 16MB in total
            for i in range(8):
                data.append(
                    (
                        "randomPrintableASCII(1024*1024)",
                        "toDateTime({})".format(
                            time_1 if i > 0 or positive else time_2
                        ),
                    )
                )

            node1.query(
                "INSERT INTO {} (s1, d1) VALUES {}".format(
                    name, ",".join(["(" + ",".join(x) + ")" for x in data])
                )
            )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}
        assert (
            "2"
            == node1.query(
                "SELECT count() FROM system.parts WHERE table = '{}' AND active = 1".format(
                    name
                )
            ).strip()
        )

        wait_expire_1_thread.join()
        time.sleep(wait_expire_2 / 2)

        node1.query("SYSTEM START MERGES {}".format(name))
        node1.query("OPTIMIZE TABLE {}".format(name))

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external" if positive else "jbod1"}
        assert (
            "1"
            == node1.query(
                "SELECT count() FROM system.parts WHERE table = '{}' AND active = 1".format(
                    name
                )
            ).strip()
        )

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "16"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine",
    [
        pytest.param(
            "mt_test_merges_with_full_disk_work",
            "MergeTree()",
            id="mt_test_merges_with_full_disk_work",
        ),
        pytest.param(
            "replicated_mt_test_merges_with_full_disk_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_merges_with_full_disk_work', '1')",
            id="replicated_mt_test_merges_with_full_disk_work",
        ),
    ],
)
def test_merges_with_full_disk_work(started_cluster, name, engine):
    name = unique_table_name(name)

    try:
        name_temp = name + "_temp"

        node1.query(
            """
            CREATE TABLE {name} (
                s1 String
            ) ENGINE = MergeTree()
            ORDER BY tuple()
            SETTINGS storage_policy='only_jbod2'
        """.format(
                name=name_temp
            )
        )

        data = []  # 35MB in total
        for i in range(35):
            data.append("randomPrintableASCII(1024*1024)")

        node1.query(
            "INSERT INTO {} VALUES {}".format(
                name_temp, ",".join(["(" + x + ")" for x in data])
            )
        )
        used_disks = get_used_disks_for_table(node1, name_temp)
        assert set(used_disks) == {"jbod2"}

        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO DISK 'jbod2'
            SETTINGS storage_policy='jbod1_with_jbod2'
        """.format(
                name=name, engine=engine
            )
        )

        wait_expire_1 = 10
        time_1 = time.time() + wait_expire_1

        wait_expire_1_thread = threading.Thread(
            target=time.sleep, args=(wait_expire_1,)
        )
        wait_expire_1_thread.start()

        for _ in range(2):
            data = []  # 12MB in total
            for i in range(6):
                data.append(
                    ("randomPrintableASCII(1024*1024)", "toDateTime({})".format(time_1))
                )  # 1MB row
            node1.query(
                "INSERT INTO {} (s1, d1) VALUES {}".format(
                    name, ",".join(["(" + ",".join(x) + ")" for x in data])
                )
            )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}
        assert (
            "2"
            == node1.query(
                "SELECT count() FROM system.parts WHERE table = '{}' AND active = 1".format(
                    name
                )
            ).strip()
        )

        wait_expire_1_thread.join()

        node1.query("OPTIMIZE TABLE {}".format(name))
        time.sleep(1)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}  # Merged to the same disk against the rule.
        assert (
            "1"
            == node1.query(
                "SELECT count() FROM system.parts WHERE table = '{}' AND active = 1".format(
                    name
                )
            ).strip()
        )

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "12"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name_temp))
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine,positive",
    [
        pytest.param(
            "mt_test_moves_after_merges_do_not_work",
            "MergeTree()",
            0,
            id="mt_test_moves_after_merges_do_not_work",
        ),
        pytest.param(
            "replicated_mt_test_moves_after_merges_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_after_merges_do_not_work', '1')",
            0,
            id="replicated_mt_test_moves_after_merges_do_not_work",
        ),
        pytest.param(
            "mt_test_moves_after_merges_work",
            "MergeTree()",
            1,
            id="mt_test_moves_after_merges_work",
        ),
        pytest.param(
            "replicated_mt_test_moves_after_merges_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_after_merges_work', '1')",
            1,
            id="replicated_mt_test_moves_after_merges_work",
        ),
    ],
)
def test_moves_after_merges_work(started_cluster, name, engine, positive):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO DISK 'external'
            SETTINGS storage_policy='small_jbod_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        wait_expire_1 = 16
        wait_expire_2 = 20
        time_1 = time.time() + wait_expire_1
        time_2 = time.time() + wait_expire_1 + wait_expire_2

        wait_expire_1_thread = threading.Thread(
            target=time.sleep, args=(wait_expire_1,)
        )
        wait_expire_1_thread.start()

        for _ in range(2):
            data = []  # 14MB in total
            for i in range(7):
                data.append(
                    (
                        "randomPrintableASCII(1024*1024)",
                        "toDateTime({})".format(
                            time_1 if i > 0 or positive else time_2
                        ),
                    )
                )  # 1MB row

            node1.query(
                "INSERT INTO {} (s1, d1) VALUES {}".format(
                    name, ",".join(["(" + ",".join(x) + ")" for x in data])
                )
            )

        node1.query("OPTIMIZE TABLE {}".format(name))

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}
        assert (
            "1"
            == node1.query(
                "SELECT count() FROM system.parts WHERE table = '{}' AND active = 1".format(
                    name
                )
            ).strip()
        )

        wait_expire_1_thread.join()
        time.sleep(wait_expire_2 / 2)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external" if positive else "jbod1"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "14"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine,positive,bar",
    [
        pytest.param(
            "mt_test_moves_after_alter_do_not_work",
            "MergeTree()",
            0,
            "DELETE",
            id="mt_negative",
        ),
        pytest.param(
            "replicated_mt_test_moves_after_alter_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_after_alter_do_not_work', '1')",
            0,
            "DELETE",
            id="repicated_negative",
        ),
        pytest.param(
            "mt_test_moves_after_alter_work",
            "MergeTree()",
            1,
            "DELETE",
            id="mt_positive",
        ),
        pytest.param(
            "replicated_mt_test_moves_after_alter_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_after_alter_work', '1')",
            1,
            "DELETE",
            id="repicated_positive",
        ),
        pytest.param(
            "mt_test_moves_after_alter_do_not_work",
            "MergeTree()",
            0,
            "TO DISK 'external'",
            id="mt_external_negative",
        ),
        pytest.param(
            "replicated_mt_test_moves_after_alter_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_after_alter_do_not_work', '1')",
            0,
            "TO DISK 'external'",
            id="replicated_external_negative",
        ),
        pytest.param(
            "mt_test_moves_after_alter_work",
            "MergeTree()",
            1,
            "TO DISK 'external'",
            id="mt_external_positive",
        ),
        pytest.param(
            "replicated_mt_test_moves_after_alter_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_moves_after_alter_work', '1')",
            1,
            "TO DISK 'external'",
            id="replicated_external_positive",
        ),
    ],
)
def test_ttls_do_not_work_after_alter(started_cluster, name, engine, positive, bar):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO DISK 'external'
            SETTINGS storage_policy='small_jbod_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        if positive:
            node1.query(
                """
                ALTER TABLE {name}
                    MODIFY TTL
                    d1 + INTERVAL 15 MINUTE {bar}
            """.format(
                    name=name, bar=bar
                )
            )  # That shall disable TTL.

        data = []  # 10MB in total
        for i in range(10):
            data.append(
                (
                    "randomPrintableASCII(1024*1024)",
                    "toDateTime({})".format(time.time() - 1),
                )
            )  # 1MB row
        node1.query(
            "INSERT INTO {} (s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1" if positive else "external"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine",
    [
        pytest.param("mt_test_materialize_ttl_in_partition", "MergeTree()", id="mt"),
        pytest.param(
            "replicated_mt_test_materialize_ttl_in_partition",
            "ReplicatedMergeTree('/clickhouse/test_materialize_ttl_in_partition', '1')",
            id="replicated",
        ),
    ],
)
def test_materialize_ttl_in_partition(started_cluster, name, engine):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                p1 Int8,
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY p1
            PARTITION BY p1
            SETTINGS storage_policy='small_jbod_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        data = []  # 5MB in total
        for i in range(5):
            data.append(
                (
                    str(i),
                    "randomPrintableASCII(1024*1024)",
                    "toDateTime({})".format(time.time() - 1),
                )
            )  # 1MB row
        node1.query(
            "INSERT INTO {} (p1, s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}

        node1.query(
            """
                ALTER TABLE {name}
                    MODIFY TTL
                    d1 TO DISK 'external' SETTINGS materialize_ttl_after_modify = 0
            """.format(
                name=name
            )
        )

        time.sleep(3)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}

        node1.query(
            """
                ALTER TABLE {name}
                    MATERIALIZE TTL IN PARTITION 2
        """.format(
                name=name
            )
        )

        node1.query(
            """
                ALTER TABLE {name}
                    MATERIALIZE TTL IN PARTITION 4
        """.format(
                name=name
            )
        )

        time.sleep(3)

        used_disks_sets = []
        for i in range(len(data)):
            used_disks_sets.append(
                set(get_used_disks_for_table(node1, name, partition=i))
            )

        assert used_disks_sets == [
            {"jbod1"},
            {"jbod1"},
            {"external"},
            {"jbod1"},
            {"external"},
        ]

        assert node1.query(
            "SELECT count() FROM {name}".format(name=name)
        ).strip() == str(len(data))

    finally:
        node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))


@pytest.mark.parametrize(
    "name,engine,positive",
    [
        pytest.param(
            "mt_test_alter_multiple_ttls_positive", "MergeTree()", True, id="positive"
        ),
        pytest.param(
            "mt_replicated_test_alter_multiple_ttls_positive",
            "ReplicatedMergeTree('/clickhouse/replicated_test_alter_multiple_ttls_positive', '1')",
            True,
            id="replicated_positive",
        ),
        pytest.param(
            "mt_test_alter_multiple_ttls_negative", "MergeTree()", False, id="negative"
        ),
        pytest.param(
            "mt_replicated_test_alter_multiple_ttls_negative",
            "ReplicatedMergeTree('/clickhouse/replicated_test_alter_multiple_ttls_negative', '1')",
            False,
            id="replicated_negative",
        ),
    ],
)
def test_alter_multiple_ttls(started_cluster, name, engine, positive):
    name = unique_table_name(name)

    """Check that when multiple TTL expressions are set
    and before any parts are inserted the TTL expressions
    are changed with ALTER command then all old
    TTL expressions are removed and the
    the parts are moved to the specified disk or volume or
    deleted if the new TTL expression is triggered
    and are not moved or deleted when it is not.
    """
    now = time.time()
    try:
        node1.query(
            """
            CREATE TABLE {name} (
                p1 Int64,
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            PARTITION BY p1
            TTL d1 + INTERVAL 34 SECOND TO DISK 'jbod2',
                d1 + INTERVAL 64 SECOND TO VOLUME 'external'
            SETTINGS storage_policy='jbods_with_external', merge_with_ttl_timeout=0
        """.format(
                name=name, engine=engine
            )
        )

        node1.query(
            """
            ALTER TABLE {name} MODIFY
            TTL d1 + INTERVAL 0 SECOND TO DISK 'jbod2',
                d1 + INTERVAL 14 SECOND TO VOLUME 'external',
                d1 + INTERVAL 19 SECOND DELETE
        """.format(
                name=name
            )
        )

        for p in range(3):
            data = []  # 6MB in total
            now = time.time()
            for i in range(2):
                p1 = p
                d1 = now - 1 if i > 0 or positive else now + 300
                data.append(
                    "({}, randomPrintableASCII(1024*1024), toDateTime({}))".format(
                        p1, d1
                    )
                )
            node1.query(
                "INSERT INTO {name} (p1, s1, d1) VALUES {values}".format(
                    name=name, values=",".join(data)
                )
            )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod2"} if positive else {"jbod1", "jbod2"}

        assert node1.query(
            "SELECT count() FROM {name}".format(name=name)
        ).splitlines() == ["6"]

        if positive:
            expected_disks = {"external"}
        else:
            expected_disks = {"jbod1", "jbod2"}

        check_used_disks_with_retry(node1, name, expected_disks, 50)

        assert node1.query(
            "SELECT count() FROM {name}".format(name=name)
        ).splitlines() == ["6"]

        time.sleep(5)

        for i in range(50):
            rows_count = int(
                node1.query("SELECT count() FROM {name}".format(name=name)).strip()
            )
            if positive:
                if rows_count == 0:
                    break
            else:
                if rows_count == 3:
                    break
            node1.query("OPTIMIZE TABLE {name} FINAL".format(name=name))
            time.sleep(0.5)

        if positive:
            assert rows_count == 0
        else:
            assert rows_count == 3

    finally:
        node1.query("DROP TABLE IF EXISTS {name} SYNC".format(name=name))


@pytest.mark.parametrize(
    "name,engine",
    [
        pytest.param("concurrently_altering_ttl_mt", "MergeTree()", id="mt"),
        pytest.param(
            "concurrently_altering_ttl_replicated_mt",
            "ReplicatedMergeTree('/clickhouse/concurrently_altering_ttl_replicated_mt', '1')",
            id="replicated_mt",
        ),
    ],
)
def test_concurrent_alter_with_ttl_move(started_cluster, name, engine):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                EventDate Date,
                number UInt64
            ) ENGINE = {engine}
            ORDER BY tuple()
            PARTITION BY toYYYYMM(EventDate)
            SETTINGS storage_policy='jbods_with_external'
        """.format(
                name=name, engine=engine
            )
        )

        values = list({random.randint(1, 1000000) for _ in range(0, 1000)})

        def insert(num):
            for i in range(num):
                day = random.randint(11, 30)
                value = values.pop()
                month = "0" + str(random.choice([3, 4]))
                node1.query(
                    "INSERT INTO {} VALUES(toDate('2019-{m}-{d}'), {v})".format(
                        name, m=month, d=day, v=value
                    )
                )

        def alter_move(num):
            def produce_alter_move(node, name):
                move_type = random.choice(["PART", "PARTITION"])
                if move_type == "PART":
                    for _ in range(10):
                        try:
                            parts = (
                                node1.query(
                                    "SELECT name from system.parts where table = '{}' and active = 1".format(
                                        name
                                    )
                                )
                                .strip()
                                .split("\n")
                            )
                            break
                        except QueryRuntimeException:
                            pass
                    else:
                        raise Exception("Cannot select from system.parts")

                    move_part = random.choice(["'" + part + "'" for part in parts])
                else:
                    move_part = random.choice([201903, 201904])

                move_disk = random.choice(["DISK", "VOLUME"])
                if move_disk == "DISK":
                    move_volume = random.choice(["'external'", "'jbod1'", "'jbod2'"])
                else:
                    move_volume = random.choice(["'main'", "'external'"])
                try:
                    node1.query(
                        "ALTER TABLE {} MOVE {mt} {mp} TO {md} {mv}".format(
                            name,
                            mt=move_type,
                            mp=move_part,
                            md=move_disk,
                            mv=move_volume,
                        )
                    )
                except QueryRuntimeException:
                    pass

            for i in range(num):
                produce_alter_move(node1, name)

        def alter_update(num):
            for i in range(num):
                try:
                    node1.query(
                        "ALTER TABLE {} UPDATE number = number + 1 WHERE 1".format(name)
                    )
                except:
                    pass

        def alter_modify_ttl(num):
            for i in range(num):
                ttls = []
                for j in range(random.randint(1, 10)):
                    what = random.choice(
                        [
                            "TO VOLUME 'main'",
                            "TO VOLUME 'external'",
                            "TO DISK 'jbod1'",
                            "TO DISK 'jbod2'",
                            "TO DISK 'external'",
                        ]
                    )
                    when = "now()+{}".format(random.randint(-1, 5))
                    ttls.append("{} {}".format(when, what))
                try:
                    node1.query(
                        "ALTER TABLE {} MODIFY TTL {}".format(name, ", ".join(ttls))
                    )
                except QueryRuntimeException:
                    pass

        def optimize_table(num):
            for i in range(num):
                try:  # optimize may throw after concurrent alter
                    node1.query(
                        "OPTIMIZE TABLE {} FINAL".format(name),
                        settings={"optimize_throw_if_noop": "1"},
                    )
                    break
                except:
                    pass

        p = Pool(15)
        tasks = []
        for i in range(5):
            tasks.append(p.apply_async(insert, (30,)))
            tasks.append(p.apply_async(alter_move, (30,)))
            tasks.append(p.apply_async(alter_update, (30,)))
            tasks.append(p.apply_async(alter_modify_ttl, (30,)))
            tasks.append(p.apply_async(optimize_table, (30,)))

        for task in tasks:
            task.get(timeout=120)

        assert node1.query("SELECT 1") == "1\n"
        assert node1.query("SELECT COUNT() FROM {}".format(name)) == "150\n"
    finally:
        node1.query("DROP TABLE IF EXISTS {name} SYNC".format(name=name))


@pytest.mark.parametrize(
    "name,engine,positive",
    [
        pytest.param("mt_test_alter_with_merge_do_not_work", "MergeTree()", 0, id="mt"),
        pytest.param(
            "replicated_mt_test_alter_with_merge_do_not_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_alter_with_merge_do_not_work', '1')",
            0,
            id="replicated",
        ),
        pytest.param("mt_test_alter_with_merge_work", "MergeTree()", 1, id="mt_work"),
        pytest.param(
            "replicated_mt_test_alter_with_merge_work",
            "ReplicatedMergeTree('/clickhouse/replicated_test_alter_with_merge_work', '1')",
            1,
            id="replicated_work",
        ),
    ],
)
def test_alter_with_merge_work(started_cluster, name, engine, positive):
    name = unique_table_name(name)

    """Check that TTL expressions are re-evaluated for
    existing parts after ALTER command changes TTL expressions
    and parts are merged.
    """
    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 + INTERVAL 3000 SECOND TO DISK 'jbod2',
                d1 + INTERVAL 6000 SECOND TO VOLUME 'external'
            SETTINGS storage_policy='jbods_with_external', merge_with_ttl_timeout=0
        """.format(
                name=name, engine=engine
            )
        )

        def optimize_table(num):
            for i in range(num):
                try:  # optimize may throw after concurrent alter
                    node1.query(
                        "OPTIMIZE TABLE {} FINAL".format(name),
                        settings={"optimize_throw_if_noop": "1"},
                    )
                    break
                except:
                    pass

        for p in range(3):
            data = []  # 6MB in total
            now = time.time()
            for i in range(2):
                d1 = now - 1 if positive else now + 300
                data.append(
                    "(randomPrintableASCII(1024*1024), toDateTime({}))".format(d1)
                )
            values = ",".join(data)
            node1.query(
                "INSERT INTO {name} (s1, d1) VALUES {values}".format(
                    name=name, values=values
                )
            )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1", "jbod2"}

        node1.query("SELECT count() FROM {name}".format(name=name)).splitlines() == [
            "6"
        ]

        node1.query(
            """
            ALTER TABLE {name} MODIFY
            TTL d1 + INTERVAL 0 SECOND TO DISK 'jbod2',
                d1 + INTERVAL 5 SECOND TO VOLUME 'external',
                d1 + INTERVAL 10 SECOND DELETE
        """.format(
                name=name
            )
        )

        optimize_table(20)

        assert (
            node1.query(
                "SELECT count() FROM system.parts WHERE table = '{name}' AND active = 1".format(
                    name=name
                )
            )
            == "1\n"
        )

        time.sleep(5)

        optimize_table(20)

        if positive:
            assert check_used_disks_with_retry(node1, name, set(["external"]), 50)
        else:
            assert check_used_disks_with_retry(node1, name, set(["jbod1", "jbod2"]), 50)

        time.sleep(5)

        optimize_table(20)

        if positive:
            assert node1.query("SELECT count() FROM {name}".format(name=name)) == "0\n"
        else:
            assert node1.query("SELECT count() FROM {name}".format(name=name)) == "6\n"

    finally:
        node1.query("DROP TABLE IF EXISTS {name} SYNC".format(name=name))


@pytest.mark.parametrize(
    "name,dest_type,engine",
    [
        pytest.param(
            "mt_test_disabled_ttl_move_on_insert_work", "DISK", "MergeTree()", id="disk"
        ),
        pytest.param(
            "mt_test_disabled_ttl_move_on_insert_work",
            "VOLUME",
            "MergeTree()",
            id="volume",
        ),
        pytest.param(
            "replicated_mt_test_disabled_ttl_move_on_insert_work",
            "DISK",
            "ReplicatedMergeTree('/clickhouse/replicated_test_disabled_ttl_move_on_insert_work', '1')",
            id="replicated_disk",
        ),
        pytest.param(
            "replicated_mt_test_disabled_ttl_move_on_insert_work",
            "VOLUME",
            "ReplicatedMergeTree('/clickhouse/replicated_test_disabled_ttl_move_on_insert_work', '1')",
            id="replicated_volume",
        ),
    ],
)
def test_disabled_ttl_move_on_insert(started_cluster, name, dest_type, engine):
    name = unique_table_name(name)

    try:
        node1.query(
            """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 TO {dest_type} 'external'
            SETTINGS storage_policy='jbod_without_instant_ttl_move'
        """.format(
                name=name, dest_type=dest_type, engine=engine
            )
        )

        node1.query("SYSTEM STOP MOVES {}".format(name))

        data = []  # 10MB in total
        for i in range(10):
            data.append(
                (
                    "randomPrintableASCII(1024*1024)",
                    "toDateTime({})".format(time.time() - 1),
                )
            )

        node1.query(
            "INSERT INTO {} (s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"jbod1"}
        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

        node1.query("SYSTEM START MOVES {}".format(name))
        time.sleep(3)

        used_disks = get_used_disks_for_table(node1, name)
        assert set(used_disks) == {"external"}
        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        try:
            node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))
        except:
            pass


@pytest.mark.parametrize(
    "name,dest_type",
    [
        pytest.param("replicated_mt_move_if_exists", "DISK", id="replicated_disk"),
        pytest.param("replicated_mt_move_if_exists", "VOLUME", id="replicated_volume"),
    ],
)
def test_ttl_move_if_exists(started_cluster, name, dest_type):
    name = unique_table_name(name)

    try:
        query_template = """
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = ReplicatedMergeTree('/clickhouse/replicated_mt_move_if_exists', '{node_name}')
            ORDER BY tuple()
            TTL d1 TO {dest_type} {if_exists} 'external'
            SETTINGS storage_policy='{policy}'
        """

        with pytest.raises(QueryRuntimeException):
            node1.query(
                query_template.format(
                    name=name,
                    node_name=node1.name,
                    dest_type=dest_type,
                    if_exists="",
                    policy="only_jbod_1",
                )
            )

        for node, policy in zip(
            [node1, node2], ["only_jbod_1", "small_jbod_with_external"]
        ):
            node.query(
                query_template.format(
                    name=name,
                    node_name=node.name,
                    dest_type=dest_type,
                    if_exists="IF EXISTS",
                    policy=policy,
                )
            )

        data = []  # 10MB in total
        for i in range(10):
            data.append(
                (
                    "randomPrintableASCII(1024*1024)",
                    "toDateTime({})".format(time.time() - 1),
                )
            )

        node1.query(
            "INSERT INTO {} (s1, d1) VALUES {}".format(
                name, ",".join(["(" + ",".join(x) + ")" for x in data])
            )
        )
        node2.query("SYSTEM SYNC REPLICA {}".format(name))

        time.sleep(5)

        used_disks1 = get_used_disks_for_table(node1, name)
        assert set(used_disks1) == {"jbod1"}

        used_disks2 = get_used_disks_for_table(node2, name)
        assert set(used_disks2) == {"external"}

        assert (
            node1.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )
        assert (
            node2.query("SELECT count() FROM {name}".format(name=name)).strip() == "10"
        )

    finally:
        try:
            node1.query("DROP TABLE IF EXISTS {} SYNC".format(name))
            node2.query("DROP TABLE IF EXISTS {} SYNC".format(name))
        except:
            pass


class TestCancelBackgroundMoving:
    @pytest.fixture()
    def prepare_table(self, request, started_cluster):
        name = unique_table_name(request.node.name)
        engine = f"ReplicatedMergeTree('/clickhouse/{name}', '1')"

        node1.query(
            f"""
            CREATE TABLE {name} (
                s1 String,
                d1 DateTime
            ) ENGINE = {engine}
            ORDER BY tuple()
            TTL d1 + interval 5 second TO DISK 'external'
            SETTINGS storage_policy='small_jbod_with_external'
            """
        )

        node1.query("SYSTEM STOP MOVES")

        # Insert part which is about to move
        node1.query(
            "INSERT INTO {} (s1, d1) VALUES (randomPrintableASCII({}), toDateTime({}))".format(
                name, 10 * 1024 * 1024, time.time()
            )
        )

        # Set low bandwidth to have enough time to cancel part moving
        config = inspect.cleandoc(
            f"""
            <clickhouse>
                <max_local_write_bandwidth_for_server>{ 256 * 1024 }</max_local_write_bandwidth_for_server>
            </clickhouse>
            """
        )
        node1.replace_config(
            "/etc/clickhouse-server/config.d/disk_throttling.xml", config
        )
        node1.restart_clickhouse()

        try:
            yield name
        finally:
            node1.query(f"DROP TABLE IF EXISTS {name} SYNC")

    def test_cancel_background_moving_on_stop_moves_query(self, prepare_table):
        name = prepare_table

        # Wait for background moving task to be started
        node1.query("SYSTEM START MOVES")
        assert_eq_with_retry(
            node1,
            f"SELECT count() FROM system.moves WHERE table = '{name}'".strip(),
            "1",
        )

        # Wait for background moving task to be cancelled
        node1.query("SYSTEM STOP MOVES")
        assert_logs_contain_with_retry(
            node1, "MergeTreeBackgroundExecutor.*Cancelled moving parts"
        )
        assert_eq_with_retry(
            node1,
            f"SELECT count() FROM system.moves WHERE table = '{name}'".strip(),
            "0",
        )

        # Ensure that part was not moved
        assert set(get_used_disks_for_table(node1, name)) == {"jbod1"}

    def test_cancel_background_moving_on_table_detach(self, prepare_table):
        name = prepare_table

        # Wait for background moving task to be started
        node1.query("SYSTEM START MOVES")
        assert_eq_with_retry(
            node1,
            f"SELECT count() FROM system.moves WHERE table = '{name}'".strip(),
            "1",
        )

        # Wait for background moving task to be cancelled
        node1.query(f"DETACH Table {name}")
        assert_logs_contain_with_retry(
            node1, "MergeTreeBackgroundExecutor.*Cancelled moving parts"
        )
        assert_eq_with_retry(
            node1,
            f"SELECT count() FROM system.moves WHERE table = '{name}'".strip(),
            "0",
        )

    def test_cancel_background_moving_on_zookeeper_disconnect(self, prepare_table):
        name = prepare_table

        # Wait for background moving task to be started
        node1.query("SYSTEM START MOVES")
        assert_eq_with_retry(
            node1,
            f"SELECT count() FROM system.moves WHERE table = '{name}'".strip(),
            "1",
        )

        with PartitionManager() as pm:
            pm.drop_instance_zk_connections(node1)
            # Wait for background moving task to be cancelled
            assert_logs_contain_with_retry(
                node1,
                "MergeTreeBackgroundExecutor.*Cancelled moving parts",
                retry_count=30,
                sleep_time=1,
            )
