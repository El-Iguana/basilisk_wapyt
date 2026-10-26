"""services.plans on captured output from each engine."""
from __future__ import annotations

MYSQL_ANALYZE = """-> Sort: n DESC  (actual time=0.0755..0.0757 rows=12 loops=1)
    -> Table scan on <temporary>  (actual time=0.0664..0.067 rows=12 loops=1)
        -> Aggregate using temporary table  (actual time=0.0659..0.0659 rows=12 loops=1)
            -> Nested loop inner join  (cost=18.7 rows=13.7) (actual time=0.0178..0.05 rows=30 loops=1)
                -> Filter: (o.`status` = 'paid')  (cost=13.9 rows=13.7) (actual time=0.0105..0.0298 rows=30 loops=1)
                    -> Table scan on o  (cost=13.9 rows=137) (actual time=0.00872..0.0243 rows=137 loops=1)
                -> Single-row index lookup on c using PRIMARY (id=o.customer_id)  (cost=0.257 rows=1) (actual time=494e-6..507e-6 rows=1 loops=30)
"""

PG = [{"Plan": {"Node Type": "Hash Join", "Join Type": "Inner", "Total Cost": 20.0,
                "Plan Rows": 10, "Actual Rows": 5, "Actual Loops": 2, "Actual Total Time": 1.5,
                "Hash Cond": "(a.id = b.a_id)", "Plans": [
                    {"Node Type": "Seq Scan", "Relation Name": "a", "Alias": "x",
                     "Total Cost": 5.0, "Plan Rows": 1000, "Actual Rows": 3, "Actual Loops": 1,
                     "Actual Total Time": 0.2, "Filter": "(x.v > 1)"}]},
       "Planning Time": 0.1, "Execution Time": 3.2}]

MSSQL = """<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan"><BatchSequence><Batch><Statements>
<StmtSimple StatementType="SELECT" StatementSubTreeCost="0.5"><QueryPlan CompileTime="2">
<RelOp NodeId="0" PhysicalOp="Nested Loops" LogicalOp="Inner Join" EstimateRows="30" EstimatedTotalSubtreeCost="0.5">
 <RunTimeInformation><RunTimeCountersPerThread Thread="0" ActualRows="30" ActualExecutions="1" ActualElapsedms="4"/></RunTimeInformation>
 <NestedLoops>
  <RelOp NodeId="1" PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan" EstimateRows="30" EstimatedTotalSubtreeCost="0.1">
   <IndexScan><Object Database="[shop]" Schema="[dbo]" Table="[orders]" Index="[PK_o]" Alias="[o]"/>
    <Predicate><ScalarOperator ScalarString="[o].[status]='paid'"/></Predicate></IndexScan>
  </RelOp>
  <RelOp NodeId="2" PhysicalOp="Clustered Index Seek" LogicalOp="Clustered Index Seek" EstimateRows="1" EstimatedTotalSubtreeCost="0.3">
   <IndexScan><Object Schema="[dbo]" Table="[customers]" Index="[PK_c]"/></IndexScan>
  </RelOp>
 </NestedLoops>
</RelOp></QueryPlan></StmtSimple></Statements></Batch></BatchSequence></ShowPlanXML>"""


def test_mysql_tree_nesting_numbers_and_labels():
    from services.plans import flatten, parse_mysql_tree

    result = parse_mysql_tree(MYSQL_ANALYZE)
    rows = flatten(result["plan"])
    assert [(r["depth"], r["op"]) for r in rows] == [
        (0, "Sort"), (1, "Table scan"), (2, "Aggregate using temporary table"),
        (3, "Nested loop inner join"), (4, "Filter"), (5, "Table scan"),
        (4, "Single-row index lookup")]
    lookup = rows[-1]
    assert lookup["object"] == "c using PRIMARY" and lookup["detail"] == "(id=o.customer_id)"
    assert lookup["loops"] == 30 and lookup["act_rows"] == 30 and lookup["est_rows"] == 1
    assert abs(lookup["time_ms"] - 507e-6 * 30) < 1e-9
    assert rows[1]["object"] == "<temporary>" and rows[4]["detail"] == "(o.`status` = 'paid')"
    assert result["summary"]["cost"] == 18.7


def test_postgresql_json():
    from services.plans import annotate, flatten, parse_postgresql

    result = annotate(parse_postgresql(PG))
    root = result["plan"]
    assert root["op"] == "Hash Join (Inner)" and root["detail"] == "Hash Cond: (a.id = b.a_id)"
    assert root["act_rows"] == 10 and root["time_ms"] == 3.0  # per loop x loops
    scan = root["children"][0]
    assert scan["object"] == "a x" and scan["detail"] == "Filter: (x.v > 1)"
    assert scan["misestimate"] and scan["share"] == 0.25
    assert root["share"] == 0.75  # its own cost: 20 minus the scan's 5
    assert result["summary"] == {"cost": 20.0, "planning_ms": 0.1, "execution_ms": 3.2}
    assert len(flatten(root)) == 2


def test_mssql_xml():
    from services.plans import parse_mssql

    result = parse_mssql(MSSQL)
    root = result["plan"]
    assert root["op"] == "Nested Loops (Inner Join)" and root["act_rows"] == 30
    assert root["loops"] == 1 and root["time_ms"] == 4
    scan, seek = root["children"]
    assert scan["object"] == "dbo.orders o using PK_o"
    assert scan["detail"] == "Where: [o].[status]='paid'" and not scan["children"]
    assert seek["object"] == "dbo.customers using PK_c" and seek["act_rows"] is None
    assert result["summary"]["cost"] == 0.5


def test_sqlite_rows():
    from services.plans import flatten, parse_sqlite

    rows = [(2, 0, 0, "SCAN o"), (5, 0, 0, "SEARCH c USING INTEGER PRIMARY KEY (rowid=?)"),
            (7, 0, 0, "CO-ROUTINE sub"), (9, 7, 0, "SCAN t"),
            (15, 0, 0, "USE TEMP B-TREE FOR ORDER BY")]
    flat = flatten(parse_sqlite(rows)["plan"])
    assert [(r["depth"], r["op"], r["object"]) for r in flat] == [
        (0, "Query plan", ""), (1, "Scan", "o"), (1, "Search", "c"), (1, "Co-routine sub", ""),
        (2, "Scan", "t"), (1, "Use temp b-tree for order by", "")]
    assert flat[2]["detail"] == "using INTEGER PRIMARY KEY (rowid=?)"


def test_mysql_classic_table():
    from services.plans import parse_mysql_table

    result = parse_mysql_table(["id", "select_type", "table", "type", "key", "rows", "Extra"],
                               [(1, "SIMPLE", "o", "ALL", None, 137, "Using where")])
    child = result["plan"]["children"][0]
    assert child["op"] == "SIMPLE ALL" and child["object"] == "o" and child["est_rows"] == 137
    assert child["detail"] == "extra: Using where"


# CockroachDB EXPLAIN ANALYZE, as captured (v26.3), with the trailing advice.
CRDB_ANALYZE = """planning time: 273µs
execution time: 1.5ms
distribution: local

• group (hash)
│ execution time: 33µs
│ actual row count: 14
│ estimated row count: 14
│ group by: name
│
└── • merge join
    │ execution time: 37µs
    │ actual row count: 137
    │ estimated row count: 137
    │ equality: (customer_id) = (id)
    │ right cols are key
    │
    ├── • scan
    │     actual row count: 137
    │     estimated row count: 137 (100% of the table; stats collected 2 seconds ago)
    │     table: orders@ix_orders_customer_id
    │     spans: FULL SCAN
    │
    └── • scan
          actual row count: 14
          estimated row count: 1,000 (100% of the table)
          table: customers@customers_pkey
          spans: FULL SCAN

index recommendations: 1
1. type: index creation
   SQL command: CREATE INDEX ON shop.public.orders (status, customer_id);""".splitlines()


def test_cockroach_text_tree():
    from services.plans import flatten, parse_cockroach

    result = parse_cockroach(CRDB_ANALYZE)
    assert result["summary"] == {"cost": None, "planning_ms": 0.273, "execution_ms": 1.5}
    rows = flatten(result["plan"])
    assert [(r["op"], r["depth"]) for r in rows] == [
        ("Group (Hash)", 0), ("Merge Join", 1), ("Scan", 2), ("Scan", 2)]
    join = rows[1]
    assert join["act_rows"] == 137 and join["time_ms"] == 0.037
    assert join["detail"] == "equality: (customer_id) = (id)"
    assert ["right cols are key", ""] in join["extra"]
    last = rows[3]
    assert last["object"] == "customers@customers_pkey" and last["est_rows"] == 1000
    # The advice after the tree belongs to no node.
    assert not any("SQL command" in key for row in rows for key, _ in row["extra"])


DUCK_ESTIMATED = [{"name": "PROJECTION", "extra_info": {"Projections": ["name"]}, "children": [
    {"name": "HASH_JOIN", "extra_info": {"Join Type": "INNER", "Conditions": "customer_id = id",
                                         "Estimated Cardinality": "~137"}, "children": [
        {"name": "SEQ_SCAN", "extra_info": {"Table": "t.main.orders", "Estimated Cardinality": "137"},
         "children": []},
        {"name": "SEQ_SCAN", "extra_info": {"Table": "t.main.customers", "Type": "Sequential Scan"},
         "children": []}]}]}]

DUCK_ANALYZED = {"latency": 0.0025, "children": [
    {"operator_type": "EXPLAIN_ANALYZE", "operator_name": "EXPLAIN_ANALYZE", "children": [
        {"operator_type": "HASH_GROUP_BY", "operator_name": "HASH_GROUP_BY",
         "operator_timing": 0.0004, "operator_cardinality": 14,
         "extra_info": {"Groups": "#0"}, "children": [
             {"operator_type": "TABLE_SCAN", "operator_name": "TABLE_SCAN",
              "operator_timing": 0.0001, "operator_cardinality": 137,
              "extra_info": {"Table": "orders"}, "children": []}]}]}]}


def test_duckdb_json_plans():
    from services.plans import flatten, parse_duckdb

    rows = flatten(parse_duckdb(DUCK_ESTIMATED)["plan"])
    assert [(r["op"], r["depth"]) for r in rows] == [
        ("Projection", 0), ("Hash Join", 1), ("Seq Scan", 2), ("Seq Scan", 2)]
    assert rows[1]["est_rows"] == 137 and "Join Type: INNER" in rows[1]["detail"]
    assert rows[2]["object"] == "t.main.orders" and rows[2]["act_rows"] is None
    assert ["Type", "Sequential Scan"] in rows[3]["extra"]
    analyzed = parse_duckdb(DUCK_ANALYZED)
    assert analyzed["summary"]["execution_ms"] == 2.5
    rows = flatten(analyzed["plan"])
    assert rows[0]["op"] == "Hash Group By" and rows[0]["act_rows"] == 14
    assert abs(rows[0]["time_ms"] - 0.4) < 1e-9 and rows[1]["object"] == "orders"
