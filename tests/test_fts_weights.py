"""bm25 column weights land on the columns they are documented against.

FTS5 assigns bm25 weights positionally across *every* column of the table, UNINDEXED ones
included, and it accepts a weight list shorter than the column count without complaint —
the remaining columns silently take the default of 1.0. Both indexes shipped that way:

    circulars_fts(circular_id UNINDEXED, title, reference, body)   -- 4 columns
    bm25(circulars_fts, 3.0, 5.0, 1.0)                             -- 3 weights

so `FTS_WEIGHTS = (3.0, 5.0, 1.0)  # title, reference, body` actually ran as
circular_id=3.0 (inert), title=5.0, reference=1.0, body=1.0. The reference column carried a
fifth of the weight it was meant to, and the body weight was never applied at all.
`laws_fts` had the identical defect.

The cost was measurable. Across eight reference-shaped queries — a circular's own reference
put to the lexical arm — only four landed in the top ten under the shipped weighting, against
eight once the columns line up and the magnitudes are tuned.

These tests pin the property rather than the numbers: one weight per column, derived from
the table's own declaration, so adding a column cannot quietly reintroduce the offset.
"""

import re
import sqlite3

import pytest

from sbpeye.search import (
    FTS_WEIGHTS,
    LAW_FTS_WEIGHTS,
    _FTS_CREATE_SQL,
    _LAW_FTS_CREATE_SQL,
    _bm25_order_by,
)


def declared_columns(create_sql: str) -> list[str]:
    """The column names an fts5 CREATE statement declares, in order."""
    inner = re.search(r"fts5\((.*)\)", create_sql, re.DOTALL).group(1)
    columns = []
    for part in inner.split(","):
        name = part.strip().split()[0]
        if name.startswith("tokenize"):
            continue
        columns.append(name)
    return columns


INDEXES = [
    pytest.param(_FTS_CREATE_SQL, FTS_WEIGHTS, "circulars_fts", id="circulars"),
    pytest.param(_LAW_FTS_CREATE_SQL, LAW_FTS_WEIGHTS, "laws_fts", id="laws"),
]


@pytest.mark.parametrize("create_sql,weights,table", INDEXES)
def test_one_weight_per_declared_column(create_sql, weights, table):
    """The bug in one assertion: three weights against four columns."""
    columns = declared_columns(create_sql)
    assert len(weights) == len(columns), (
        f"{table} declares {len(columns)} columns {columns} but carries "
        f"{len(weights)} bm25 weights {weights}; FTS5 would apply them from the left and "
        "default the rest"
    )


@pytest.mark.parametrize("create_sql,weights,table", INDEXES)
def test_the_unindexed_id_column_is_weighted_zero(create_sql, weights, table):
    """The leading column is UNINDEXED and contributes nothing; say so in the number.

    A non-zero weight there is inert, so it cannot be caught by behaviour — only by
    reading it. Which is exactly how it went unnoticed.
    """
    first = declared_columns(create_sql)[0]
    assert first.endswith("_id")
    assert weights[0] == 0.0, f"{table}: {first} is UNINDEXED, weight it 0.0"


@pytest.mark.parametrize("create_sql,weights,table", INDEXES)
def test_the_order_by_carries_every_weight(create_sql, weights, table):
    order_by = _bm25_order_by(table, weights)
    emitted = order_by[order_by.index("(") + 1:order_by.rindex(")")].split(",")
    assert len(emitted) == len(declared_columns(create_sql)) + 1  # +1 for the table name
    assert emitted[0].strip() == table


def test_the_builder_follows_the_tuple_rather_than_a_fixed_arity():
    """A column added to the table and the tuple must not need a format string edited."""
    assert _bm25_order_by("t", (0.0, 1.0)) == "bm25(t, 0, 1)"
    assert _bm25_order_by("t", (0.0, 2.5, 1.0, 0.5)) == "bm25(t, 0, 2.5, 1, 0.5)"


def test_a_title_match_outranks_a_body_match_when_title_is_weighted():
    """The behaviour the weights exist for, on a table small enough to reason about.

    Pinned because the offset bug was invisible in behaviour on the real corpus: at the
    shipped magnitudes of 3 and 5 the ranking barely moved either way, so nothing failed
    and nothing looked wrong. It only shows up once the weights are far enough apart to
    dominate, which is what this builds.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE t USING fts5(doc_id UNINDEXED, title, body, "
        "tokenize='unicode61')"
    )
    conn.executemany(
        "INSERT INTO t (doc_id, title, body) VALUES (?, ?, ?)",
        [
            ("title-match", "housing finance", "unrelated text about deposit accounts"),
            ("body-match", "unrelated heading", "housing finance " * 40),
        ],
    )

    def first(weights):
        order_by = _bm25_order_by("t", weights)
        return conn.execute(
            f"SELECT doc_id FROM t WHERE t MATCH 'housing OR finance' ORDER BY {order_by}"
        ).fetchone()[0]

    assert first((0.0, 100.0, 1.0)) == "title-match"
    assert first((0.0, 1.0, 100.0)) == "body-match"
    conn.close()


def test_shifting_the_weights_left_changes_the_ranking():
    """What the shipped code did, shown as a behaviour difference rather than a count.

    The same three numbers against the same table rank differently depending on whether a
    weight is supplied for the UNINDEXED column, because every weight after the omission
    is applied to the wrong column.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE t USING fts5(doc_id UNINDEXED, title, body, "
        "tokenize='unicode61')"
    )
    conn.executemany(
        "INSERT INTO t (doc_id, title, body) VALUES (?, ?, ?)",
        [
            ("title-match", "housing finance", "unrelated text about deposit accounts"),
            ("body-match", "unrelated heading", "housing finance " * 40),
        ],
    )

    def order(sql_weights):
        return [
            row[0] for row in conn.execute(
                "SELECT doc_id FROM t WHERE t MATCH 'housing OR finance' "
                f"ORDER BY bm25(t, {sql_weights})"
            )
        ]

    # Intended: title=1, body=100 -> the body match wins.
    assert order("0, 1, 100")[0] == "body-match"
    # Shipped shape: the same numbers minus the id weight. 1 lands on title, 100 on body's
    # left neighbour — there is none, so body defaults to 1 and the title match wins.
    assert order("1, 100")[0] == "title-match"
    conn.close()
