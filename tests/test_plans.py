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
