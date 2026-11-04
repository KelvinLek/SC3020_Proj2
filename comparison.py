"""
comparison.py - compares PostgreSQL's query execution plan (QEP) with the plan
an AI agent predicts for the same query.

Input:  two PlanNode trees from preprocessing.py
          pg_tree = get_postgres_plan(conn, sql, parallel=False)
          ai_tree = get_ai_plan(conn, sql, schema_info=schema)
Output: a ComparisonResult (see compare_plans) with
          * annotated copies of both trees: every node gets a status and colour
            (same / changed / unmatched / helper), a match id linking it to its
            partner in the other tree, and a short note for tooltips;
          * the list of differences, each with a category and an explanation;
          * the similarities;
          * the difference rate (normalised tree edit distance).

The algorithm has five steps:

  1. Core trees. PostgreSQL adds helper nodes that only carry out a decision
     made elsewhere: Hash (build phase of a Hash Join), Memoize, Materialize,
     Gather, a Sort that a Merge Join / GroupAggregate / Unique needs, and
     partial aggregates. These are spliced out of a "core" copy of each tree,
     so the comparison looks at decisions (access methods, join algorithms,
     join order, aggregation), not at executor plumbing. A Bitmap Heap Scan
     and its Bitmap Index Scan children are one access method and are folded
     into one core node. Helpers are still shown (grey) and reported.

  2. Matching. Nodes are matched by what they compute, never by position or
     by schema-specific rules:
       * scans by the table they read (several scans of one table, e.g. a
         self-join, are paired by alias and filter similarity);
       * joins by the multiset of base tables beneath them;
       * other operators (aggregates, sorts, limits, ...) by their kind and the
         tables beneath them, then by tree position for what is left.

  3. Classification. Each matched pair is checked for differences in access
     method, join algorithm, join type, input order, aggregation strategy and
     conditions; unmatched nodes become join-order, missing or extra nodes.

  4. Explanations. Each difference gets a sentence built from the plan itself
     (PostgreSQL's row estimates, index use) and, if given, the table sizes
     from get_schema(), plus the AI's own stated reason.

  5. Difference rate. A weighted Zhang-Shasha tree edit distance between the
     two core trees, divided by the total number of core nodes, so 0 means
     the same plan and 1 means nothing in common. A "strict" rate on the full
     trees (helper nodes included) is reported alongside.
"""

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from preprocessing import PlanNode


# =============================================================================
# Statuses, colours and difference categories (shared with the GUI)
# =============================================================================

STATUS_SAME = "same"            # matched, and the operator/table/index agree
STATUS_CHANGED = "changed"      # matched, but a significant property differs
STATUS_UNMATCHED = "unmatched"  # no counterpart in the other plan
STATUS_HELPER = "helper"        # PostgreSQL helper node, not counted

STATUS_COLOURS = {
    STATUS_SAME: "#4CAF50",       # green
    STATUS_CHANGED: "#FFB300",    # amber
    STATUS_UNMATCHED: "#E53935",  # red
    STATUS_HELPER: "#9E9E9E",     # grey
}

STATUS_LABELS = {
    STATUS_SAME: "Same in both plans",
    STATUS_CHANGED: "In both plans, but different",
    STATUS_UNMATCHED: "Only in this plan",
    STATUS_HELPER: "Helper node (not counted)",
}

# Difference categories.
ACCESS_METHOD = "ACCESS_METHOD"          # Seq Scan vs Index Scan, different index
JOIN_ALGORITHM = "JOIN_ALGORITHM"        # Hash Join vs Nested Loop vs Merge Join
JOIN_ORDER = "JOIN_ORDER"                # different grouping, or inputs swapped
JOIN_TYPE = "JOIN_TYPE"                  # Inner vs Semi vs Anti vs Left ...
AGGREGATION = "AGGREGATION"              # HashAggregate vs GroupAggregate ...
OTHER_OPERATOR = "OTHER_OPERATOR"        # e.g. Sort vs Incremental Sort
MISSING_NODE = "MISSING_NODE"            # in PostgreSQL's plan only
EXTRA_NODE = "EXTRA_NODE"                # in the AI's plan only
CONDITION = "CONDITION"                  # filter / join condition differs
INVALID_REFERENCE = "INVALID_REFERENCE"  # AI named a table/index/operator that does not exist
HELPER_NODE = "HELPER_NODE"              # helper node in one plan only

CATEGORY_TITLES = {
    ACCESS_METHOD: "Access method",
    JOIN_ALGORITHM: "Join algorithm",
    JOIN_ORDER: "Join order",
    JOIN_TYPE: "Join type",
    AGGREGATION: "Aggregation",
    OTHER_OPERATOR: "Other operator",
    MISSING_NODE: "Missing in AI plan",
    EXTRA_NODE: "Extra in AI plan",
    CONDITION: "Condition",
    INVALID_REFERENCE: "Invalid reference",
    HELPER_NODE: "Helper node",
}
_CATEGORY_ORDER = list(CATEGORY_TITLES)

# Severities: "major" differences make a node amber/red; "minor" ones
# (conditions, swapped Merge Join inputs) and "info" ones (helper nodes) are
# listed but do not change the node's colour.
MAJOR, MINOR, INFO = "major", "minor", "info"
_SEVERITY_ORDER = {MAJOR: 0, MINOR: 1, INFO: 2}


# =============================================================================
# Operator groups (PostgreSQL vocabulary only, no schema knowledge)
# =============================================================================

JOIN_OPERATORS = {"Nested Loop", "Hash Join", "Merge Join"}
GROUP_OPERATORS = {"Aggregate", "HashAggregate", "GroupAggregate", "MixedAggregate", "Unique"}
SORT_OPERATORS = {"Sort", "Incremental Sort"}
BITMAP_PARTS = {"Bitmap Index Scan", "BitmapAnd", "BitmapOr"}

# Always helpers: they implement a decision taken by their parent.
HELPER_OPERATORS = {"Hash", "Memoize", "Materialize", "Gather", "Gather Merge"}
# A Sort directly below one of these is implied by the parent's algorithm.
# A Sort anywhere else (e.g. for ORDER BY) is a real operator.
NEEDS_SORTED_INPUT = {"Merge Join", "GroupAggregate", "Unique", "WindowAgg",
                      "SetOp", "Incremental Sort"}
SUBPLAN_RELATIONSHIPS = {"InitPlan", "SubPlan"}

# Edit costs for the tree edit distance. Insert/delete cost 1. Renaming a
# node costs 1 if it becomes a different kind of operator (e.g. a scan
# becomes a join, or a scan of another table), 0.5 for another algorithm of
# the same kind (Seq Scan -> Index Scan, Hash Join -> Nested Loop) and 0.25
# when only a detail differs (the index used, the join type).
COST_INSERT_DELETE = 1.0
COST_OTHER_KIND = 1.0
COST_OTHER_METHOD = 0.5
COST_OTHER_DETAIL = 0.25


# =============================================================================
# Result structures
# =============================================================================

@dataclass
class Difference:
    """One difference between the two plans.

    pg_nodes / ai_nodes are the nodes (in the annotated trees of the result)
    the difference is about; match_id links a matched pair (None otherwise).
    """
    category: str
    severity: str
    explanation: str
    pg_nodes: List[PlanNode] = field(default_factory=list)
    ai_nodes: List[PlanNode] = field(default_factory=list)
    match_id: Optional[int] = None

    @property
    def title(self) -> str:
        return CATEGORY_TITLES.get(self.category, self.category)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "explanation": self.explanation,
            "match_id": self.match_id,
            "pg_nodes": [n.details.get("cmp_node_id") for n in self.pg_nodes],
            "ai_nodes": [n.details.get("cmp_node_id") for n in self.ai_nodes],
        }


@dataclass
class ComparisonResult:
    """Everything compare_plans() finds. pg_tree and ai_tree are annotated
    copies; the trees passed in are left unchanged."""
    pg_tree: PlanNode
    ai_tree: PlanNode
    differences: List[Difference]
    similarities: List[str]
    difference_rate: float          # normalised TED on the core trees, 0..1
    edit_distance: float
    pg_size: int                    # core nodes in each tree
    ai_size: int
    strict_difference_rate: float   # same, on the full trees (helpers included)
    strict_edit_distance: float
    pg_strict_size: int
    ai_strict_size: int
    matched_same: int               # matched pairs with no major difference
    matched_changed: int            # matched pairs with a major difference
    unmatched_pg: int               # core nodes only in PostgreSQL's plan
    unmatched_ai: int               # core nodes only in the AI's plan

    @property
    def major_differences(self) -> List[Difference]:
        return [d for d in self.differences if d.severity == MAJOR]

    @property
    def category_counts(self) -> Dict[str, int]:
        """Number of differences per category (all severities)."""
        counts = Counter(d.category for d in self.differences)
        return {c: counts[c] for c in _CATEGORY_ORDER if counts[c]}

    @property
    def node_agreement(self) -> float:
        """Share of core nodes that have an identical partner: 1 = all agree."""
        total = self.pg_size + self.ai_size
        return 1.0 if total == 0 else 2 * self.matched_same / total

    @property
    def summary(self) -> str:
        """A few sentences for the top of the results panel."""
        major = self.major_differences
        lines = []
        if not major:
            lines.append("The AI predicted the same plan as PostgreSQL"
                         + (" (apart from helper nodes or conditions)." if self.differences else "."))
        else:
            counts = Counter(d.category for d in major)
            parts = [f"{n} {CATEGORY_TITLES[c].lower()}" for c, n in
                     sorted(counts.items(), key=lambda kv: _CATEGORY_ORDER.index(kv[0]))]
            lines.append(f"The plans differ in {len(major)} significant way(s): " + ", ".join(parts) + ".")
        lines.append(
            f"Difference rate: {self.difference_rate:.2f} (tree edit distance "
            f"{self.edit_distance:g} over {self.pg_size + self.ai_size} operators); "
            f"{self.strict_difference_rate:.2f} if helper nodes are counted.")
        lines.append(
            f"{self.matched_same} operator(s) agree, {self.matched_changed} differ, "
            f"{self.unmatched_pg} only in PostgreSQL's plan, {self.unmatched_ai} only in the AI's plan.")
        return "\n".join(lines)

    def to_dict(self, include_trees: bool = False) -> Dict[str, Any]:
        """Plain dict for JSON logs (e.g. the batch experiments)."""
        data = {
            "difference_rate": round(self.difference_rate, 4),
            "edit_distance": self.edit_distance,
            "pg_size": self.pg_size,
            "ai_size": self.ai_size,
            "strict_difference_rate": round(self.strict_difference_rate, 4),
            "strict_edit_distance": self.strict_edit_distance,
            "pg_strict_size": self.pg_strict_size,
            "ai_strict_size": self.ai_strict_size,
            "node_agreement": round(self.node_agreement, 4),
            "matched_same": self.matched_same,
            "matched_changed": self.matched_changed,
            "unmatched_pg": self.unmatched_pg,
            "unmatched_ai": self.unmatched_ai,
            "category_counts": self.category_counts,
            "differences": [d.to_dict() for d in self.differences],
            "similarities": list(self.similarities),
            "summary": self.summary,
        }
        if include_trees:
            data["pg_tree"] = _tree_to_dict(self.pg_tree)
            data["ai_tree"] = _tree_to_dict(self.ai_tree)
        return data

    def report(self) -> str:
        """Plain-text report: summary, both annotated trees, differences, similarities."""
        out = [self.summary, "", "--- PostgreSQL plan", format_tree(self.pg_tree),
               "", "--- AI plan", format_tree(self.ai_tree), "", "Differences:"]
        if not self.differences:
            out.append("  none")
        for i, d in enumerate(self.differences, 1):
            tag = f" #{d.match_id}" if d.match_id else ""
            out.append(f"  {i}. [{d.title}, {d.severity}{tag}] {d.explanation}")
        out.append("")
        out.append("Similarities:")
        out.extend(f"  - {s}" for s in self.similarities) if self.similarities else out.append("  none")
        return "\n".join(out)


# =============================================================================
# Public entry point
# =============================================================================

def compare_plans(pg_tree: PlanNode, ai_tree: PlanNode,
                  schema_info: Optional[Dict[str, Any]] = None) -> ComparisonResult:
    """Compare PostgreSQL's plan with the AI's plan for the same query.

    schema_info (optional) is get_schema(conn); when given, explanations can
    quote table sizes and say whether an index could have been used.
    """
    if pg_tree is None or ai_tree is None:
        raise ValueError("compare_plans needs two plan trees.")

    # Work on copies so the caller's trees are never modified.
    pg_tree = copy.deepcopy(pg_tree)
    ai_tree = copy.deepcopy(ai_tree)
    _number_nodes(pg_tree, "pg")
    _number_nodes(ai_tree, "ai")

    # Step 1: core trees.
    pg_root = _build_core_tree(pg_tree, "pg")
    ai_root = _build_core_tree(ai_tree, "ai")
    pg_cores = list(_core_postorder(pg_root))
    ai_cores = list(_core_postorder(ai_root))

    # Step 2: matching.
    _match(pg_cores, ai_cores)
    match_id = 0
    for core in pg_cores:
        if core.match is not None:
            match_id += 1
            core.match_id = core.match.match_id = match_id

    # Steps 3 and 4: classification and explanations.
    ctx = _Context(schema_info, pg_cores, ai_cores)
    differences: List[Difference] = []
    similarities: List[str] = []
    for core in pg_cores:
        if core.match is not None:
            _compare_pair(core, core.match, ctx, differences, similarities)
    _explain_unmatched(pg_cores, ai_cores, ctx, differences)
    _compare_helpers(pg_cores, ctx, differences)
    _check_invalid_references(ai_cores, differences)
    similarities.extend(_overall_similarities(pg_cores, ai_cores, differences))
    similarities = list(dict.fromkeys(similarities))     # drop repeated sentences

    # Write status, colour, match id and note into every node.
    _annotate(pg_tree, pg_cores)
    _annotate(ai_tree, ai_cores)

    differences.sort(key=lambda d: (_SEVERITY_ORDER[d.severity],
                                    _CATEGORY_ORDER.index(d.category),
                                    d.match_id or 10 ** 6))

    # Step 5: difference rate.
    ted = tree_edit_distance(pg_root, ai_root, lambda c: c.children, _core_label)
    strict_ted = tree_edit_distance(pg_tree, ai_tree, lambda n: n.children, _plain_label)
    pg_strict, ai_strict = _count(pg_tree), _count(ai_tree)

    return ComparisonResult(
        pg_tree=pg_tree,
        ai_tree=ai_tree,
        differences=differences,
        similarities=similarities,
        difference_rate=_normalise(ted, len(pg_cores), len(ai_cores)),
        edit_distance=ted,
        pg_size=len(pg_cores),
        ai_size=len(ai_cores),
        strict_difference_rate=_normalise(strict_ted, pg_strict, ai_strict),
        strict_edit_distance=strict_ted,
        pg_strict_size=pg_strict,
        ai_strict_size=ai_strict,
        matched_same=sum(1 for c in pg_cores if c.match and c.status == STATUS_SAME),
        matched_changed=sum(1 for c in pg_cores if c.match and c.status == STATUS_CHANGED),
        unmatched_pg=sum(1 for c in pg_cores if c.match is None),
        unmatched_ai=sum(1 for c in ai_cores if c.match is None),
    )


def status_colour(node: PlanNode) -> str:
    """Colour for a node of an annotated tree (for the GUI)."""
    return STATUS_COLOURS.get(node.details.get("cmp_status"), "#FFFFFF")


def format_tree(node: PlanNode, indent: int = 0) -> str:
    """Indented text view of an annotated tree, with each node's status."""
    status = node.details.get("cmp_status", "?")
    mid = node.details.get("cmp_match_id")
    tag = f"[{status}{' #' + str(mid) if mid else ''}]"
    line = "  " * indent + tag + " " + node.pretty().splitlines()[0]
    return "\n".join([line] + [format_tree(c, indent + 1) for c in node.children])


# =============================================================================
# Step 1: core trees (helper nodes spliced out, bitmap scans folded)
# =============================================================================

class _Core:
    """A node of the core tree: one real operator of the plan."""

    def __init__(self, node: PlanNode, side: str):
        self.node = node
        self.side = side                       # "pg" or "ai"
        self.children: List["_Core"] = []
        self.parent: Optional["_Core"] = None
        self.absorbed: List[PlanNode] = []     # bitmap parts folded into this node
        self.helpers_above: List[PlanNode] = []  # helpers between this node and its core parent
        self.is_subplan = False                # root of an InitPlan / SubPlan
        self.in_subplan = False                # anywhere inside an InitPlan / SubPlan
        self.family = _family(node.operator)
        self.tables: Tuple[str, ...] = ()      # multiset of base tables beneath (sorted)
        self.indexes: Tuple[str, ...] = ()     # indexes used by a scan (lower case)
        self.match: Optional["_Core"] = None
        self.match_id: Optional[int] = None
        self.status: Optional[str] = None
        self.notes: List[str] = []

    @property
    def operator(self) -> str:
        return self.node.operator

    @property
    def inputs(self) -> List["_Core"]:
        """Children that are real inputs (not InitPlan/SubPlan)."""
        return [c for c in self.children if not c.is_subplan]

    def describe(self) -> str:
        """Short description, e.g. 'Seq Scan on lineitem' or 'Hash Join of {a, b}'."""
        if self.family == "scan":
            text = f"{self.operator} on {self.node.table or '?'}"
            indexes = _display_indexes(self)
            return text + (f" using {indexes}" if indexes else "")
        if self.family == "join":
            return f"{self.operator} of {_fmt_tables(self.tables)}"
        if self.tables:
            return f"{self.operator} over {_fmt_tables(self.tables)}"
        return self.operator

    def __repr__(self):  # for debugging
        return f"<{self.side} {self.describe()}>"


def _family(operator: str) -> str:
    """Kind of operator: scan, join, group, sort, or the operator itself."""
    if operator in JOIN_OPERATORS:
        return "join"
    if operator in GROUP_OPERATORS:
        return "group"
    if operator in SORT_OPERATORS:
        return "sort"
    if operator.endswith("Scan") and operator != "Subquery Scan":
        return "scan"
    return operator


def _is_helper(node: PlanNode, core_parent_op: Optional[str]) -> bool:
    """True for nodes that only implement a decision made by their parent."""
    if not node.children:          # a helper always wraps an input
        return False
    if node.operator in HELPER_OPERATORS:
        return True
    if node.details.get("partial_mode") == "Partial":   # parallel partial aggregate
        return True
    if node.details.get("added_by_normalization"):
        return True
    return node.operator in SORT_OPERATORS and core_parent_op in NEEDS_SORTED_INPUT


def _build_core_tree(root: PlanNode, side: str) -> "_Core":
    cores = _build_core(root, side, None, [], False)
    if len(cores) != 1:   # root is a helper with several inputs: keep it
        cores = _build_core(root, side, None, [], False, force_core=True)
    core_root = cores[0]
    for core in _core_postorder(core_root):
        _fill_tables(core)
    _mark_in_subplan(core_root, False)
    return core_root


def _mark_in_subplan(core: "_Core", inherited: bool) -> None:
    core.in_subplan = inherited or core.is_subplan
    for child in core.children:
        _mark_in_subplan(child, core.in_subplan)


def _build_core(node: PlanNode, side: str, core_parent_op: Optional[str],
                helpers: List[PlanNode], in_subplan: bool,
                force_core: bool = False) -> List["_Core"]:
    """Core node(s) for `node`'s subtree. A helper node returns the cores of
    its children (it is spliced out); any other node returns one core."""
    in_subplan = in_subplan or node.details.get("parent_relationship") in SUBPLAN_RELATIONSHIPS
    if not force_core and _is_helper(node, core_parent_op):
        result = []
        for child in node.children:
            result.extend(_build_core(child, side, core_parent_op, helpers + [node], in_subplan))
        return result

    core = _Core(node, side)
    core.helpers_above = helpers
    core.is_subplan = in_subplan
    for child in node.children:
        is_bitmap_part = (node.operator == "Bitmap Heap Scan" and child.operator in BITMAP_PARTS
                          and child.details.get("parent_relationship") not in SUBPLAN_RELATIONSHIPS)
        if is_bitmap_part:
            core.absorbed.extend(child.walk())
        else:
            core.children.extend(_build_core(child, side, node.operator, [], False))
    for child in core.children:
        child.parent = core
    return [core]


def _fill_tables(core: "_Core") -> None:
    """Tables beneath a node (children already filled). Sub-plans are left
    out: they are evaluated separately, not joined in."""
    if core.family == "scan":
        table = core.node.table
        core.tables = (table.lower(),) if table else ()
        names = [core.node.index] + [n.index for n in core.absorbed]
        core.indexes = tuple(sorted({n.lower() for n in names if n}))
    else:
        core.tables = tuple(sorted(t for c in core.inputs for t in c.tables))


def _core_postorder(core: "_Core"):
    for child in core.children:
        yield from _core_postorder(child)
    yield core


# =============================================================================
# Step 2: matching
# =============================================================================

def _match(pg_cores: List["_Core"], ai_cores: List["_Core"]) -> None:
    """Pair up nodes of the two core trees (sets core.match on both sides)."""
    # Pass 1: same kind of operator over the same multiset of tables.
    # For scans that means the same table; for joins the same set of joined
    # tables, whatever the join order below them.
    groups_ai: Dict[tuple, List[_Core]] = {}
    for core in ai_cores:
        groups_ai.setdefault((core.family, core.tables), []).append(core)
    groups_pg: Dict[tuple, List[_Core]] = {}
    for core in pg_cores:
        groups_pg.setdefault((core.family, core.tables), []).append(core)

    for key, pg_group in groups_pg.items():
        ai_group = groups_ai.get(key, [])
        if not ai_group:
            continue
        if key[0] == "scan" and (len(pg_group) > 1 or len(ai_group) > 1):
            # Several scans of one table (self-join, sub-query): pair the
            # most similar ones first.
            _greedy_pairs(pg_group, ai_group, _scan_similarity)
        else:
            # Otherwise pair bottom-up (both lists are in post-order).
            for a, b in zip(pg_group, ai_group):
                _link(a, b)

    # Pass 2: remaining non-scan, non-join operators of the same kind (e.g.
    # the final Aggregate when one plan reads an extra table), paired by
    # table overlap and by already-matched neighbours.
    left_pg = [c for c in pg_cores if c.match is None and c.family not in ("scan", "join")]
    left_ai = [c for c in ai_cores if c.match is None and c.family not in ("scan", "join")]

    def neighbour_score(a: _Core, b: _Core) -> float:
        if a.family != b.family:
            return -1.0
        score = _multiset_jaccard(a.tables, b.tables)
        if a.parent and b.parent and a.parent.match is b.parent:
            score += 0.5
        if any(ca.match is not None and ca.match in b.children for ca in a.children):
            score += 0.5
        if not a.tables and not b.tables:
            score += 0.5
        return score if score > 0 else -1.0

    _greedy_pairs(left_pg, left_ai, neighbour_score)


def _link(a: "_Core", b: "_Core") -> None:
    a.match, b.match = b, a


def _greedy_pairs(pg_list, ai_list, score_fn) -> None:
    """Link pairs in order of decreasing score (score < 0 means 'never')."""
    candidates = []
    for i, a in enumerate(pg_list):
        for j, b in enumerate(ai_list):
            score = score_fn(a, b)
            if score >= 0:
                candidates.append((-score, i, j))
    candidates.sort()
    for _, i, j in candidates:
        a, b = pg_list[i], ai_list[j]
        if a.match is None and b.match is None:
            _link(a, b)


def _scan_similarity(a: "_Core", b: "_Core") -> float:
    score = 0.0
    alias_a, alias_b = a.node.details.get("alias"), b.node.details.get("alias")
    if alias_a and alias_b and alias_a.lower() == alias_b.lower():
        score += 2.0
    score += _set_jaccard({p.norm for p in _predicates(a.node.filter)},
                          {p.norm for p in _predicates(b.node.filter)})
    if a.operator == b.operator:
        score += 0.25
    if a.in_subplan == b.in_subplan:
        score += 0.25
    if a.parent is not None and b.parent is not None and a.parent.family == b.parent.family:
        score += 0.1
    return score


def _multiset_jaccard(x: tuple, y: tuple) -> float:
    if not x and not y:
        return 0.0
    cx, cy = Counter(x), Counter(y)
    return sum((cx & cy).values()) / sum((cx | cy).values())


def _set_jaccard(x: set, y: set) -> float:
    if not x and not y:
        return 1.0
    return len(x & y) / len(x | y)


# =============================================================================
# Condition handling
# =============================================================================
# Conditions are compared as sets of conjuncts (the predicates joined by AND).
# Each conjunct is normalised so that writing differences do not count:
# quotes, casing, spaces, parentheses, table prefixes, "date '...'" literals,
# PostgreSQL's ~~ for LIKE, and the side of = / < / > the column is on.

@dataclass(frozen=True)
class _Predicate:
    raw: str     # as written
    norm: str    # normalised text: equal norms mean the same predicate
    shape: str   # norm without literals: equal shapes mean "same column and
                 # comparison, value written differently"


def _predicates(condition: Optional[str]) -> List[_Predicate]:
    if not condition:
        return []
    return [_Predicate(raw=p.strip(), norm=_normalise_predicate(p), shape=_predicate_shape(p))
            for p in _split_conjuncts(condition) if p.strip()]


def _strip_outer_parens(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for i, ch in enumerate(text):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and i < len(text) - 1:
                return text          # the first "(" closes before the end
        text = text[1:-1].strip()
    return text


def _split_conjuncts(condition: str) -> List[str]:
    """Split a condition at its top-level ANDs (not inside parentheses,
    quotes or BETWEEN ... AND ...)."""
    text = _strip_outer_parens(condition)
    lower = text.lower()
    parts, start, depth, in_quote, pending_between = [], 0, 0, False, False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and re.match(r"\bbetween\b", lower[i:]) and (i == 0 or not lower[i - 1].isalnum()):
                pending_between = True
            elif depth == 0 and re.match(r"\sand\s", lower[i:i + 5]):
                if pending_between:
                    pending_between = False
                else:
                    parts.append(text[start:i])
                    start = i + 5
                    i += 5
                    continue
        i += 1
    parts.append(text[start:])
    if len(parts) == 1:
        return [text]
    result = []
    for part in parts:
        result.extend(_split_conjuncts(part))
    return result


_FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "=", "<>": "<>"}
_QUALIFIER = re.compile(r"\b[a-z_][a-z0-9_$]*\.(?=[a-z_\"])")


def _canonical_comparison(text: str) -> str:
    """Write 'a op b' with the smaller side first, so 'x = y' == 'y = x'
    and 'a < 5' == '5 > a'."""
    m = re.fullmatch(r"(.+?)(<=|>=|<>|=|<|>)(.+)", text)
    if not m:
        return text
    left, op, right = m.groups()
    if left > right:
        left, op, right = right, _FLIP[op], left
    return f"{left}{op}{right}"


def _normalise_predicate(pred: str) -> str:
    t = pred.lower()
    t = re.sub(r"\b(date|timestamp)\s+'", "'", t)                # date '1995-01-01'
    t = re.sub(r"(\d{4}-\d{2}-\d{2}) 00:00:00", r"\1", t)          # midnight timestamps
    t = (t.replace("!~~*", " not ilike ").replace("~~*", " ilike ")
          .replace("!~~", " not like ").replace("~~", " like ").replace("!=", "<>"))
    t = _QUALIFIER.sub("", t)                                       # orders.o_custkey
    t = t.replace("'", "").replace('"', "")
    t = re.sub(r"[()\s]", "", t)
    return _canonical_comparison(t)


def _predicate_shape(pred: str) -> str:
    t = pred.lower()
    t = re.sub(r"'[^']*'", "", t)                                   # string / date literals
    t = re.sub(r"\b\d+(\.\d+)?\b", "", t)                            # numbers
    t = re.sub(r"\b(date|timestamp|interval|years?|months?|days?|hours?|minutes?|seconds?)\b", "", t)
    t = (t.replace("!~~", " not like ").replace("~~", " like ").replace("!=", "<>"))
    t = _QUALIFIER.sub("", t)
    t = re.sub(r"[()\s+\-*/\"]", "", t)
    return _canonical_comparison(t)


# =============================================================================
# Steps 3 and 4: classification and explanations
# =============================================================================

class _Context:
    """Lookups shared by the explanation functions."""

    def __init__(self, schema_info, pg_cores, ai_cores):
        self.schema = {str(k).lower(): v for k, v in (schema_info or {}).items()}
        self.pg_cores = pg_cores
        self.ai_cores = ai_cores
        self.pg_has_subplans = any(c.is_subplan for c in pg_cores)

    def table_info(self, table: Optional[str]) -> Optional[Dict[str, Any]]:
        return self.schema.get(table.lower()) if table else None

    def where_else(self, pred: _Predicate, cores: List["_Core"], exclude: "_Core") -> Optional["_Core"]:
        """Another node of the same plan that applies this predicate."""
        for core in cores:
            if core is exclude:
                continue
            for cond in (core.node.filter, core.node.join_condition):
                if any(p.norm == pred.norm for p in _predicates(cond)):
                    return core
        return None


def _add(differences, category, severity, explanation, a=None, b=None, match_id=None):
    differences.append(Difference(
        category=category, severity=severity, explanation=explanation,
        pg_nodes=[a.node] if a is not None else [],
        ai_nodes=[b.node] if b is not None else [],
        match_id=match_id))


def _compare_pair(a: "_Core", b: "_Core", ctx: _Context,
                  differences: List[Difference], similarities: List[str]) -> None:
    """Classify the differences of a matched pair (a from PostgreSQL, b from the AI)."""
    found: List[Tuple[str, str, str, str]] = []   # (category, severity, explanation, note)

    if a.family == "scan":
        if a.operator != b.operator or a.indexes != b.indexes:
            found.append((ACCESS_METHOD, MAJOR, _explain_access(a, b, ctx),
                          f"Access method differs: {_method_phrase(a)} vs {_method_phrase(b)}."))
    elif a.family == "join":
        if a.operator != b.operator:
            found.append((JOIN_ALGORITHM, MAJOR, _explain_join_algorithm(a, b),
                          f"Join algorithm differs: {a.operator} vs {b.operator}."))
        jt_a, jt_b = _join_type(a), _join_type(b)
        if jt_a != jt_b:
            found.append((JOIN_TYPE, MAJOR, _explain_join_type(a, b, jt_a, jt_b),
                          f"Join type differs: {jt_a} vs {jt_b}."))
        if _inputs_swapped(a, b):
            # Input order matters when both use the same algorithm (which side
            # is hashed, which side drives the loop); for a Merge Join, or when
            # the algorithms already differ, it is only a minor point.
            severity = MAJOR if a.operator == b.operator != "Merge Join" else MINOR
            found.append((JOIN_ORDER, severity, _explain_swap(a, b),
                          "The two inputs are in the opposite order."))
    elif a.family == "group":
        if a.operator != b.operator:
            found.append((AGGREGATION, MAJOR, _explain_aggregation(a, b),
                          f"Aggregation differs: {a.operator} vs {b.operator}."))
    elif a.operator != b.operator:
        found.append((OTHER_OPERATOR, MAJOR,
                      f"PostgreSQL uses a {a.operator} over {_fmt_tables(a.tables)}, "
                      f"while the AI uses a {b.operator}.{_ai_reason(b)}",
                      f"Operator differs: {a.operator} vs {b.operator}."))

    found.extend(_compare_conditions(a, b, ctx))

    major = any(sev == MAJOR for _, sev, _, _ in found)
    a.status = b.status = STATUS_CHANGED if major else STATUS_SAME
    if not found:
        note = "Same operator in both plans."
        a.notes.append(note)
        b.notes.append(note)
    for category, severity, explanation, note in found:
        _add(differences, category, severity, explanation, a, b, a.match_id)
        a.notes.append(note)
        b.notes.append(note)
    if not major:
        similarities.append(_similarity_sentence(a))


def _compare_conditions(a: "_Core", b: "_Core", ctx: _Context):
    """Minor differences in the filter and join conditions of a matched pair."""
    found = []
    pairs = [("filter", a.node.filter, b.node.filter)]
    # Join conditions are only comparable when both joins combine the same
    # two inputs; after a different join order they differ necessarily.
    if a.family != "join" or _same_input_split(a, b):
        pairs.append(("join condition", a.node.join_condition, b.node.join_condition))
    for label, cond_a, cond_b in pairs:
        preds_a, preds_b = _predicates(cond_a), _predicates(cond_b)
        norms_a, norms_b = {p.norm for p in preds_a}, {p.norm for p in preds_b}
        if norms_a == norms_b:
            continue
        where = a.describe()
        if label == "join condition" and not preds_b:
            text = (f"The AI gives no join condition for its {b.operator} of {_fmt_tables(a.tables)}; "
                    f"PostgreSQL joins on {cond_a}.")
            found.append((CONDITION, MINOR, text, "The AI gives no join condition."))
            continue
        only_a = [p for p in preds_a if p.norm not in norms_b]
        only_b = [p for p in preds_b if p.norm not in norms_a]
        # Same column and comparison, value written differently
        # (e.g. '1993-12-01' vs '1993-11-01' + interval '1' month).
        shapes_b = {p.shape for p in only_b if p.shape}
        rewritten = [p for p in only_a if p.shape and p.shape in shapes_b]
        if rewritten and len(rewritten) == len(only_a) == len(only_b):
            text = (f"The {label} of the {where} is written differently: PostgreSQL has {cond_a}, "
                    f"the AI has {cond_b}. They test the same column(s) and are probably equivalent.")
            found.append((CONDITION, MINOR, text, f"The {label} is written differently."))
            continue
        pieces = [f"The {label} of the {where} differs. PostgreSQL: {cond_a or 'none'}; "
                  f"AI: {cond_b or 'none'}."]
        for p in only_a:
            other = ctx.where_else(p, ctx.ai_cores, b)
            if other is not None:
                pieces.append(f"The AI applies {p.raw} at its {other.describe()} instead.")
        for p in only_b:
            other = ctx.where_else(p, ctx.pg_cores, a)
            if other is not None:
                pieces.append(f"PostgreSQL applies {p.raw} at its {other.describe()} instead.")
        found.append((CONDITION, MINOR, " ".join(pieces), f"The {label} differs."))
    return found


def _join_type(core: "_Core") -> str:
    return (core.node.details.get("join_type") or "Inner").title()


def _same_input_split(a: "_Core", b: "_Core") -> bool:
    """Do two joins combine the same two inputs (in either order)?"""
    split_a = sorted(c.tables for c in a.inputs)
    split_b = sorted(c.tables for c in b.inputs)
    return split_a == split_b


def _inputs_swapped(a: "_Core", b: "_Core") -> bool:
    ia, ib = a.inputs, b.inputs
    if len(ia) != 2 or len(ib) != 2 or ia[0].tables == ia[1].tables:
        return False
    return ia[0].tables == ib[1].tables and ia[1].tables == ib[0].tables


# --- explanation texts --------------------------------------------------------

def _rows(core: Optional["_Core"]) -> Optional[int]:
    value = core.node.details.get("estimated_rows") if core is not None else None
    return int(round(value)) if isinstance(value, (int, float)) else None


def _fmt_rows(n: Optional[int]) -> str:
    if n is None:
        return "an unknown number of rows"
    return f"about {n:,} row" + ("" if n == 1 else "s")


def _fmt_share(share: float) -> str:
    return "under 0.1%" if 0 < share < 0.001 else f"{share:.1%}"


def _fmt_tables(tables) -> str:
    return "{" + ", ".join(tables) + "}" if tables else "{}"


def _display_indexes(core: "_Core") -> str:
    names = [core.node.index] + [n.index for n in core.absorbed]
    seen = []
    for name in names:
        if name and name.lower() not in [s.lower() for s in seen]:
            seen.append(name)
    return " + ".join(seen)


def _table_ref(core: "_Core") -> str:
    """'region', or 'region (alias r1)' when the query gives it an alias."""
    table = core.node.table or "?"
    alias = core.node.details.get("alias")
    if alias and alias.lower() != table.lower():
        return f"{table} (alias {alias})"
    return table


def _is_inner_lookup(core: "_Core") -> bool:
    """Is this scan the inner input of a Nested Loop (looked up once per outer row)?"""
    parent = core.parent
    return (parent is not None and parent.operator == "Nested Loop"
            and len(parent.inputs) == 2 and parent.inputs[1] is core
            and core.operator != "Seq Scan")


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _method_phrase(core: "_Core") -> str:
    """'a Seq Scan', 'an Index Scan on idx_x', 'a Bitmap Heap Scan on idx_a + idx_b'."""
    text = f"{_article(core.operator)} {core.operator}"
    indexes = _display_indexes(core)
    return text + (f" using {indexes}" if indexes else "")


def _ai_reason(b: "_Core") -> str:
    reason = b.node.details.get("ai_reason")
    return f' The AI\'s reason: "{reason}"' if reason else ""


def _estimate_note(a: "_Core", b: "_Core") -> str:
    ra, rb = _rows(a), _rows(b)
    if ra is None or rb is None:
        return ""
    low, high = sorted((max(ra, 1), max(rb, 1)))
    if high / low < 10:
        return ""
    per_loop = " per outer row" if _is_inner_lookup(a) else ""
    return f" PostgreSQL estimates {_fmt_rows(ra)}{per_loop} here; the AI estimates {rb:,}."


def _index_could_apply(ctx: _Context, core: "_Core") -> Optional[bool]:
    """Does any index of the table start with a column used in the node's
    filter? None if the schema is unknown."""
    info = ctx.table_info(core.node.table)
    if info is None:
        return None
    condition = (core.node.filter or "").lower()
    for ix in info.get("indexes", []):
        cols = ix.get("columns") or []
        if cols and re.search(rf"\b{re.escape(str(cols[0]).lower())}\b", condition):
            return True
    return False


def _selectivity_text(core: "_Core", ctx: _Context) -> Tuple[str, Optional[float]]:
    rows = _rows(core)
    info = ctx.table_info(core.node.table)
    total = info["stats"].get("tuples") if info else None
    if rows is None:
        return "", None
    if total:
        share = min(rows / total, 1.0)
        return f"about {rows:,} of the table's {total:,} rows, or {_fmt_share(share)},", share
    return f"about {rows:,} rows", None


def _explain_access(a: "_Core", b: "_Core", ctx: _Context) -> str:
    table = _table_ref(a)
    if a.operator == b.operator:
        text = (f"Both plans read {table} with {_article(a.operator)} {a.operator}, but with a different "
                f"index: PostgreSQL uses {_display_indexes(a) or 'none'}, the AI uses "
                f"{_display_indexes(b) or 'none'}.")
    else:
        text = f"PostgreSQL reads {table} with {_method_phrase(a)}, while the AI uses {_method_phrase(b)}."

    sel_text, share = _selectivity_text(a, ctx)
    info = ctx.table_info(a.node.table)
    blocks = info["stats"].get("blocks") if info else None
    parent_op = a.parent.operator if a.parent is not None else None

    reason = ""
    if a.operator == "Seq Scan":
        usable = _index_could_apply(ctx, a)
        if not a.node.filter:
            reason = (" PostgreSQL has no selection on this table that an index could use, so every row "
                      "is needed and reading the table sequentially is cheapest.")
        elif usable is False:
            reason = (f" No index of {a.node.table} starts with a column used in PostgreSQL's filter on it, "
                      "so an index cannot speed up this selection.")
        elif sel_text:
            reason = (f" PostgreSQL expects {sel_text} to pass the filter. When this many rows "
                      "qualify, reading every page in order is estimated to be cheaper than many "
                      "random look-ups through an index.")
    elif _is_inner_lookup(a) and a.parent.node.details.get("index_nested_loop"):
        reason = (f" PostgreSQL uses the index to look up the matching {a.node.table} rows for each row of "
                  "the outer input of the Nested Loop above (an index nested-loop join).")
    elif a.operator in ("Index Scan", "Index Only Scan"):
        if share is not None and share < 0.2:
            reason = (f" PostgreSQL expects only {sel_text} to qualify, so fetching them through the "
                      "index is estimated to be cheaper than reading"
                      + (f" all {blocks:,} pages of the table." if blocks else " the whole table."))
        elif parent_op == "Merge Join":
            reason = (" The index returns the rows already sorted on the join key, as the Merge Join "
                      "above needs, so no separate sort is required.")
        elif a.operator == "Index Scan":
            reason = (" Reading through the index returns the rows in index order, which lets PostgreSQL "
                      "avoid a separate sort.")
        if a.operator == "Index Only Scan":
            reason += (" All columns the query needs from this table are in the index, so PostgreSQL reads "
                       "the (smaller) index instead of the table.")
    elif a.operator == "Bitmap Heap Scan":
        reason = (" A bitmap scan first collects the locations of all matching rows from the index, "
                  "then reads each needed page once, in physical order. PostgreSQL prefers it when too "
                  "many rows qualify for a plain index scan but too few for a full scan"
                  + (f"; here it expects {sel_text} to qualify." if sel_text else "."))
        if len(a.indexes) > 1:
            reason += f" It combines {len(a.indexes)} indexes (BitmapAnd / BitmapOr)."

    if b.operator != "Seq Scan" and _is_inner_lookup(b):
        reason += (f" In the AI plan, {b.node.table} is the inner input of a Nested Loop, so the index is "
                   "used to look up its matching rows for each outer row.")
    if b.node.details.get("unknown_index"):
        reason += f" The index the AI names, {b.node.index}, does not exist."
    return text + reason + _estimate_note(a, b) + _ai_reason(b)


def _join_reason(a: "_Core") -> str:
    inputs = a.inputs
    if len(inputs) != 2:
        return ""
    outer, inner = inputs
    if a.operator == "Hash Join":
        return (f" PostgreSQL builds a hash table on {_fmt_tables(inner.tables)} ({_fmt_rows(_rows(inner))}) "
                f"and probes it with the rows of {_fmt_tables(outer.tables)} ({_fmt_rows(_rows(outer))}): "
                "one pass over each input, which suits large inputs joined on equality.")
    if a.operator == "Nested Loop":
        if a.node.details.get("index_nested_loop"):
            return (f" PostgreSQL expects {_fmt_rows(_rows(outer))} from {_fmt_tables(outer.tables)} and, for "
                    f"each of them, looks up the matching rows of {_fmt_tables(inner.tables)} through an index, "
                    "which is cheap when the outer input is small.")
        return (f" PostgreSQL expects {_fmt_rows(_rows(outer))} from {_fmt_tables(outer.tables)} and compares "
                "each of them with the inner input; this is chosen when the outer input is very small or "
                "the join condition is not an equality.")
    if a.operator == "Merge Join":
        return (" PostgreSQL merges the two inputs sorted on the join key, which is cheap when they are "
                "already sorted (e.g. read in index order).")
    return ""


def _explain_join_algorithm(a: "_Core", b: "_Core") -> str:
    return (f"PostgreSQL joins {_fmt_tables(a.tables)} with {_article(a.operator)} {a.operator}, while the AI "
            f"uses {_article(b.operator)} {b.operator}.{_join_reason(a)}{_estimate_note(a, b)}{_ai_reason(b)}")


_JOIN_TYPE_MEANING = {
    "Semi": "a Semi join returns each row of the outer input once if it has a match (as for EXISTS / IN)",
    "Anti": "an Anti join returns the rows of the outer input that have no match (as for NOT EXISTS)",
    "Left": "a Left join keeps outer rows without a match, padded with NULLs",
    "Right": "a Right join keeps inner rows without a match, padded with NULLs",
    "Full": "a Full join keeps unmatched rows of both inputs",
}


def _explain_join_type(a, b, jt_a, jt_b) -> str:
    text = (f"PostgreSQL performs the {a.operator} of {_fmt_tables(a.tables)} as {_article(jt_a)} {jt_a} join, "
            f"while the AI makes it {_article(jt_b)} {jt_b} join.")
    meanings = [_JOIN_TYPE_MEANING[t] for t in (jt_a, jt_b) if t in _JOIN_TYPE_MEANING]
    if meanings:
        text += " Here, " + "; ".join(meanings) + "."
    if {jt_a, jt_b} == {"Left", "Right"}:
        text += " With the inputs swapped, these mean the same thing."
    return text + _ai_reason(b)


def _explain_swap(a: "_Core", b: "_Core") -> str:
    outer, inner = a.inputs
    tables = _fmt_tables(a.tables)
    if a.operator != b.operator:
        return (f"Besides using a different join algorithm for {tables}, the plans put its inputs in the "
                f"opposite order: PostgreSQL's first (outer) input is {_fmt_tables(outer.tables)}, the AI's "
                f"is {_fmt_tables(inner.tables)}.")
    if a.operator == "Hash Join":
        text = (f"Both plans use a Hash Join for {tables}, but with the inputs swapped: PostgreSQL builds "
                f"the hash table on {_fmt_tables(inner.tables)} ({_fmt_rows(_rows(inner))}) and probes it "
                f"with {_fmt_tables(outer.tables)} ({_fmt_rows(_rows(outer))}), while the AI builds it on "
                f"{_fmt_tables(outer.tables)}. PostgreSQL normally hashes the input it expects to be smaller.")
    elif a.operator == "Nested Loop":
        text = (f"Both plans use a Nested Loop for {tables}, but PostgreSQL loops over "
                f"{_fmt_tables(outer.tables)} and looks up {_fmt_tables(inner.tables)} for each row, while the "
                f"AI loops over {_fmt_tables(inner.tables)} instead.")
    else:
        text = (f"Both plans use {_article(a.operator)} {a.operator} for {tables}, but with the left and right "
                "inputs swapped.")
    return text + _ai_reason(b)


_AGG_PHRASES = {
    "Aggregate": "a plain Aggregate (one result over all input rows)",
    "HashAggregate": "a HashAggregate (groups rows in an in-memory hash table; no sort needed)",
    "GroupAggregate": "a GroupAggregate (groups rows that arrive sorted on the grouping key)",
    "MixedAggregate": "a MixedAggregate (hashing and sorting for several grouping sets)",
    "Unique": "a Unique node (removes duplicates from sorted input)",
}


def _explain_aggregation(a: "_Core", b: "_Core") -> str:
    text = (f"PostgreSQL groups the rows with {_AGG_PHRASES.get(a.operator, a.operator)}, while the AI uses "
            f"{_AGG_PHRASES.get(b.operator, b.operator)}.")
    groups = _rows(a)
    if groups is not None and a.operator != "Aggregate":
        text += f" PostgreSQL expects about {groups:,} groups."
    return text + _estimate_note(a, b) + _ai_reason(b)


_NODE_PURPOSE = {
    "Sort": "sorts its input",
    "Incremental Sort": "finishes sorting input that is already partly sorted",
    "Limit": "stops after the first rows",
    "Unique": "removes duplicate rows",
    "Aggregate": "computes an aggregate over all rows",
    "HashAggregate": "groups rows with a hash table",
    "GroupAggregate": "groups sorted rows",
    "Subquery Scan": "reads the result of a sub-query",
    "CTE Scan": "reads the result of a WITH query",
    "Result": "computes values without reading a table, or checks a constant condition",
    "Append": "concatenates the results of several inputs (e.g. UNION ALL)",
    "SetOp": "computes INTERSECT / EXCEPT",
    "WindowAgg": "computes window functions",
    "Materialize": "stores its input so it can be re-read",
}


def _purpose(core: "_Core") -> str:
    purpose = _NODE_PURPOSE.get(core.operator)
    if core.operator in SORT_OPERATORS and core.node.details.get("sort_key"):
        purpose = f"sorts by {', '.join(map(str, core.node.details['sort_key']))}"
    return f" (it {purpose})" if purpose else ""


def _explain_unmatched(pg_cores, ai_cores, ctx: _Context, differences: List[Difference]) -> None:
    """Join-order, missing and extra nodes for the unmatched core nodes."""
    for core in pg_cores + ai_cores:
        if core.match is None:
            core.status = STATUS_UNMATCHED

    pg_joins = [c for c in pg_cores if c.match is None and c.family == "join"]
    ai_joins = [c for c in ai_cores if c.match is None and c.family == "join"]

    if pg_joins and ai_joins:
        # Both plans form intermediate results the other never forms: the
        # tables are joined in a different order.
        def sequence(cores):
            joins = [c for c in cores if c.family == "join" and not c.is_subplan]
            return "; ".join(f"{i}) {c.operator} {_fmt_tables(c.tables)}" for i, c in enumerate(joins, 1))

        text = (f"The plans join the tables in a different order. PostgreSQL: {sequence(pg_cores)}. "
                f"AI: {sequence(ai_cores)}. Only PostgreSQL forms "
                + ", ".join(_fmt_tables(c.tables) + (f" ({_fmt_rows(_rows(c))})" if _rows(c) is not None else "")
                            for c in pg_joins)
                + "; only the AI forms " + ", ".join(_fmt_tables(c.tables) for c in ai_joins) + "."
                + " PostgreSQL's cost-based optimiser usually joins first the tables it expects to give "
                  "the smallest intermediate result.")
        if ctx.pg_has_subplans:
            text += (" Part of PostgreSQL's plan is evaluated as a separate sub-plan (InitPlan / SubPlan) "
                     "rather than as a join.")
        differences.append(Difference(JOIN_ORDER, MAJOR, text,
                                      pg_nodes=[c.node for c in pg_joins],
                                      ai_nodes=[c.node for c in ai_joins]))
        for c in pg_joins:
            c.notes.append(f"Join order differs: the AI never joins exactly {_fmt_tables(c.tables)}.")
        for c in ai_joins:
            c.notes.append(f"Join order differs: PostgreSQL never joins exactly {_fmt_tables(c.tables)}.")
        handled = set(map(id, pg_joins + ai_joins))
    else:
        handled = set()

    for core in pg_cores:
        if core.match is not None or id(core) in handled:
            continue
        if core.family == "scan":
            text = f"PostgreSQL reads {core.node.table} ({core.operator}), but the AI plan never reads this table."
        elif core.family == "join":
            text = (f"PostgreSQL joins {_fmt_tables(core.tables)} with {_article(core.operator)} {core.operator}, "
                    "but the AI plan never joins exactly these tables.")
        else:
            text = f"PostgreSQL's plan has {_article(core.operator)} {core.describe()}{_purpose(core)} that the AI plan does not have."
        _add(differences, MISSING_NODE, MAJOR, text, a=core)
        core.notes.append("Only in PostgreSQL's plan.")

    for core in ai_cores:
        if core.match is not None or id(core) in handled:
            continue
        if _invalid_flags(core):
            core.notes.append("Only in the AI plan, and it refers to something that does not exist.")
            continue      # reported as INVALID_REFERENCE
        if core.family == "scan":
            text = f"The AI plan reads {core.node.table} ({core.operator}), but PostgreSQL's plan never reads this table."
        elif core.family == "join":
            text = (f"The AI joins {_fmt_tables(core.tables)} with {_article(core.operator)} {core.operator}, "
                    "but PostgreSQL's plan never joins exactly these tables.")
            if ctx.pg_has_subplans:
                text += (" PostgreSQL evaluates part of the query as a separate sub-plan "
                         "(InitPlan / SubPlan) instead of a join.")
        else:
            text = f"The AI plan has {_article(core.operator)} {core.describe()}{_purpose(core)} that PostgreSQL's plan does not have."
        _add(differences, EXTRA_NODE, MAJOR, text + _ai_reason(core), b=core)
        core.notes.append("Only in the AI's plan.")


_HELPER_PURPOSE = {
    "Hash": "it builds the in-memory hash table for the Hash Join above",
    "Memoize": "it caches the results of repeated inner look-ups of the Nested Loop above",
    "Materialize": "it stores its input in memory so the parent can re-read it cheaply",
    "Gather": "it collects the rows produced by parallel worker processes",
    "Gather Merge": "it collects sorted rows from parallel workers, keeping them in order",
    "Sort": "it sorts the input, because the operator above needs sorted input",
    "Incremental Sort": "it sorts the input, because the operator above needs sorted input",
}


def _helper_purpose(node: PlanNode) -> str:
    if node.details.get("partial_mode") == "Partial":
        return "it computes partial aggregates inside each parallel worker"
    if node.details.get("added_by_normalization") and node.operator not in _HELPER_PURPOSE:
        return "it was added to the AI plan to follow PostgreSQL's layout"
    return _HELPER_PURPOSE.get(node.operator, "it supports the operator above")


def _compare_helpers(pg_cores, ctx: _Context, differences: List[Difference]) -> None:
    """Helper nodes above matched operators that only one plan has.
    Reported for information; they do not count towards the difference rate."""
    for a in pg_cores:
        b = a.match
        if b is None or not _same_position(a, b):
            continue      # helpers then differ only because the structure does
        ops_a = Counter(h.operator for h in a.helpers_above)
        ops_b = Counter(h.operator for h in b.helpers_above)
        for side, helpers, missing, owner in (("PostgreSQL", a.helpers_above, ops_a - ops_b, a),
                                               ("The AI", b.helpers_above, ops_b - ops_a, b)):
            for op in missing:
                node = next(h for h in helpers if h.operator == op)
                who = "the AI plan" if side == "PostgreSQL" else "PostgreSQL's plan"
                text = (f"{side} adds {_article(op)} {op} node above the {owner.describe()} "
                        f"({_helper_purpose(node)}); {who} does not. Helper nodes are not counted "
                        "in the difference rate.")
                differences.append(Difference(
                    HELPER_NODE, INFO, text,
                    pg_nodes=[node] if side == "PostgreSQL" else [],
                    ai_nodes=[node] if side != "PostgreSQL" else [],
                    match_id=a.match_id))


def _same_position(a: "_Core", b: "_Core") -> bool:
    """Are two matched nodes the same input of matched parents (or both roots)?"""
    if a.parent is None or b.parent is None:
        return a.parent is None and b.parent is None
    if a.parent.match is not b.parent:
        return False
    inputs_a, inputs_b = a.parent.inputs, b.parent.inputs
    if a in inputs_a and b in inputs_b:
        return inputs_a.index(a) == inputs_b.index(b)
    return True


def _invalid_flags(core: "_Core") -> List[str]:
    """Problems the AI-plan normaliser flagged on this node (or its bitmap parts)."""
    problems = []
    for node in [core.node] + core.absorbed:
        d = node.details
        if d.get("unknown_table"):
            problems.append(f"the table '{node.table}', which does not exist in the database")
        if d.get("unknown_index"):
            problems.append(f"the index '{node.index}', which does not exist in the database")
        if d.get("index_wrong_table"):
            problems.append(f"the index '{node.index}' for table {node.table}, but that index belongs to another table")
        if d.get("unknown_operator"):
            problems.append(f"the operator '{node.operator}', which PostgreSQL does not have")
    return problems


def _check_invalid_references(ai_cores, differences: List[Difference]) -> None:
    for core in ai_cores:
        problems = _invalid_flags(core)
        if not problems:
            continue
        text = "The AI plan refers to " + "; and to ".join(problems) + "."
        _add(differences, INVALID_REFERENCE, MAJOR, text, a=core.match, b=core, match_id=core.match_id)
        if core.match is not None and core.status == STATUS_SAME:
            core.status = core.match.status = STATUS_CHANGED
        core.notes.append("Refers to a table, index or operator that does not exist.")


def _similarity_sentence(a: "_Core") -> str:
    if a.family == "scan":
        indexes = _display_indexes(a)
        return f"Both plans read {_table_ref(a)} with {_article(a.operator)} {a.operator}" + (
            f" using {indexes}." if indexes else ".")
    if a.family == "join":
        return f"Both plans join {_fmt_tables(a.tables)} with {_article(a.operator)} {a.operator}."
    if a.family == "group":
        return f"Both plans aggregate with {_article(a.operator)} {a.operator}."
    return f"Both plans have {_article(a.operator)} {a.describe()}."


def _overall_similarities(pg_cores, ai_cores, differences) -> List[str]:
    lines = []
    tables_pg = sorted(t for c in pg_cores if c.family == "scan" for t in c.tables)
    tables_ai = sorted(t for c in ai_cores if c.family == "scan" for t in c.tables)
    if tables_pg and tables_pg == tables_ai:
        lines.append(f"Both plans read the same tables: {_fmt_tables(tables_pg)}.")
    joins_pg = [c for c in pg_cores if c.family == "join"]
    if len(joins_pg) > 1 and not any(d.category in (JOIN_ORDER, MISSING_NODE, EXTRA_NODE)
                                     and any(n.operator in JOIN_OPERATORS for n in d.pg_nodes + d.ai_nodes)
                                     for d in differences):
        lines.append("Both plans join the tables in the same order.")
    return lines


# --- annotation ---------------------------------------------------------------

def _annotate(tree: PlanNode, cores: List["_Core"]) -> None:
    """Write cmp_status, cmp_colour, cmp_match_id and cmp_note into every node."""
    covered = set()
    for core in cores:
        note = " ".join(dict.fromkeys(core.notes)) or STATUS_LABELS[core.status]
        _set(core.node, core.status, core.match_id, note)
        covered.add(id(core.node))
        for part in core.absorbed:
            _set(part, core.status, core.match_id, "Part of the bitmap scan above (compared together with it).")
            covered.add(id(part))
    for node in tree.walk():
        if id(node) not in covered:
            _set(node, STATUS_HELPER, None,
                 f"Helper node: {_helper_purpose(node)}. Not counted in the difference rate.")


def _set(node: PlanNode, status: str, match_id: Optional[int], note: str) -> None:
    node.details["cmp_status"] = status
    node.details["cmp_colour"] = STATUS_COLOURS[status]
    node.details["cmp_match_id"] = match_id
    node.details["cmp_note"] = note


def _number_nodes(tree: PlanNode, prefix: str) -> None:
    """Give every node a unique id, e.g. 'pg-3', in pre-order."""
    counter = 0

    def visit(node):
        nonlocal counter
        counter += 1
        node.details["cmp_node_id"] = f"{prefix}-{counter}"
        for child in node.children:
            visit(child)

    visit(tree)


def _tree_to_dict(node: PlanNode) -> Dict[str, Any]:
    data = node.to_dict()
    data["details"] = {k: v for k, v in node.details.items() if k != "ai_raw_response"}
    data["children"] = [_tree_to_dict(c) for c in node.children]
    return data


def _count(tree: PlanNode) -> int:
    return sum(1 for _ in tree.walk())


# =============================================================================
# Step 5: tree edit distance (Zhang-Shasha) and the difference rate
# =============================================================================

def _core_label(core: "_Core") -> tuple:
    return _label(core.family, core.operator, core.tables[0] if core.family == "scan" and core.tables else None,
                  core.indexes, _join_type(core) if core.family == "join" else None)


def _plain_label(node: PlanNode) -> tuple:
    family = _family(node.operator)
    join_type = (node.details.get("join_type") or "Inner").title() if family == "join" else None
    table = node.table.lower() if family == "scan" and node.table else None
    indexes = (node.index.lower(),) if family == "scan" and node.index else ()
    return _label(family, node.operator, table, indexes, join_type)


def _label(family, operator, table, indexes, join_type) -> tuple:
    return (family, operator, table, tuple(indexes), join_type)


def relabel_cost(x: tuple, y: tuple) -> float:
    """Cost of turning one operator label into another (see COST_*)."""
    if x == y:
        return 0.0
    family_x, op_x, table_x = x[0], x[1], x[2]
    family_y, op_y, table_y = y[0], y[1], y[2]
    if family_x != family_y or table_x != table_y:
        return COST_OTHER_KIND
    if op_x != op_y:
        return COST_OTHER_METHOD
    return COST_OTHER_DETAIL


def tree_edit_distance(t1, t2, children: Callable, label: Callable,
                       relabel: Callable[[Any, Any], float] = relabel_cost,
                       indel: float = COST_INSERT_DELETE) -> float:
    """Zhang-Shasha tree edit distance between two ordered trees.

    The cheapest sequence of node insertions, deletions and relabellings that
    turns t1 into t2. Children order matters, which suits query plans: the
    outer and inner inputs of a join play different roles.

    Nodes are numbered in post-order; l(i) is the leftmost leaf below node i.
    The key roots are the nodes with no ancestor sharing their l(i) value; for
    each pair of key roots a forest-distance table is filled in, and subtree
    distances found on the way are stored in td. Time O(|T1| |T2| min(depth1,
    leaves1) min(depth2, leaves2)), space O(|T1| |T2|).
    """
    nodes1, left1 = _postorder_with_leftmost(t1, children)
    nodes2, left2 = _postorder_with_leftmost(t2, children)
    labels1 = [label(n) for n in nodes1]
    labels2 = [label(n) for n in nodes2]
    n, m = len(nodes1), len(nodes2)
    if n == 0 or m == 0:
        return float(n + m) * indel

    keyroots1 = sorted({l: i for i, l in enumerate(left1)}.values())
    keyroots2 = sorted({l: j for j, l in enumerate(left2)}.values())
    td = [[0.0] * m for _ in range(n)]

    for i in keyroots1:
        for j in keyroots2:
            li, lj = left1[i], left2[j]
            rows, cols = i - li + 2, j - lj + 2
            fd = [[0.0] * cols for _ in range(rows)]
            for x in range(1, rows):
                fd[x][0] = fd[x - 1][0] + indel
            for y in range(1, cols):
                fd[0][y] = fd[0][y - 1] + indel
            for x in range(1, rows):
                i1 = li + x - 1
                for y in range(1, cols):
                    j1 = lj + y - 1
                    if left1[i1] == li and left2[j1] == lj:
                        # Both forests are whole trees: relabel the roots.
                        fd[x][y] = min(fd[x - 1][y] + indel, fd[x][y - 1] + indel,
                                       fd[x - 1][y - 1] + relabel(labels1[i1], labels2[j1]))
                        td[i1][j1] = fd[x][y]
                    else:
                        p, q = left1[i1] - li, left2[j1] - lj
                        fd[x][y] = min(fd[x - 1][y] + indel, fd[x][y - 1] + indel,
                                       fd[p][q] + td[i1][j1])
    return td[n - 1][m - 1]


def _postorder_with_leftmost(root, children: Callable):
    nodes, leftmost = [], []

    def visit(node):
        first_leaf = None
        for child in children(node):
            index = visit(child)
            if first_leaf is None:
                first_leaf = leftmost[index]
        nodes.append(node)
        leftmost.append(first_leaf if first_leaf is not None else len(nodes) - 1)
        return len(nodes) - 1

    if root is not None:
        visit(root)
    return nodes, leftmost


def _normalise(distance: float, size1: int, size2: int) -> float:
    """Difference rate = TED / (|T1| + |T2|), in [0, 1]: deleting every node
    of T1 and inserting every node of T2 is always possible, so the distance
    never exceeds |T1| + |T2|."""
    total = size1 + size2
    return 0.0 if total == 0 else min(distance / total, 1.0)
