"""General utilities."""

import collections
import functools
import re
import typing

from drudge import prod_, TensorDef
from jinja2 import (
    Environment, PackageLoader, ChoiceLoader, DictLoader, contextfilter
)
from sympy import Expr, Symbol, Poly, Integer, Mul, poly_from_expr, Number, oo


#
# Cost-related utilities.
#

def get_cost_key(cost: Expr) -> typing.Tuple[int, typing.List[Expr]]:
    """Get the key for ordering the cost.

    The cost should be a polynomial of at most one undetermined variable.  The
    result gives ordering of the cost agreeing with our common sense.
    """

    symbs = cost.atoms(Symbol)
    n_symbs = len(symbs)

    if n_symbs == 0:
        return 0, [cost]
    elif n_symbs == 1:
        symb = symbs.pop()
        coeffs = Poly(cost, symb).all_coeffs()
        return len(coeffs) - 1, coeffs
    else:
        raise ValueError(
            'Invalid cost to compare', cost,
            'expecting univariate polynomial or number'
        )


def is_positive_cost(cost: Expr):
    """Decide if a cost is positive."""
    _, coeffs = get_cost_key(cost)
    for i in coeffs:
        if i.has(oo):
            return i > 0
    return coeffs[0] > 0


def add_costs(*args):
    """Add the arguments as costs.

    Here when one of the operand is unity, it will be taken as a zero in the
    summation.
    """

    res = sum(i if abs(i) != 1 else 0 for i in args)
    return res if res != 0 else 1


def get_total_size(sums) -> Expr:
    """Get the total size of a summation list."""

    size = Integer(1)
    for _, i in sums:
        curr = i.size
        if curr is None:
            raise ValueError(
                'Invalid range for optimization', i,
                'expecting a bound range.'
            )
        size *= curr
        continue

    if isinstance(size, Expr):
        return size
    elif isinstance(size, int):
        return Integer(size)
    else:
        raise TypeError('Invalid total size', size, 'from sums', sums)


@functools.total_ordering
class Tuple4Cmp(tuple):
    """Simple tuple for comparison.

    Everything is the same as the built-in tuple class, just the equality and
    ordering is solely based on the first item.

    Note that this class does not make any advanced checking of the validity of
    the comparison.
    """

    def __eq__(self, other):
        """Make equality comparison."""
        return self[0] == other[0]

    def __lt__(self, other):
        """Make less-than comparison."""
        return self[0] < other[0]


#
# Public cost computation function.
#


def get_flop_cost(
        eval_seq: typing.Iterable[TensorDef], leading=False,
        ignore_consts=True
):
    """Get the FLOP cost for the given evaluation sequence.

    This function gives the count of floating-point operations, addition and
    multiplication, involved by the evaluation sequence.  Note that the cost of
    copying and initialization are not counted.  And this function is only
    applicable where the amplitude of the terms are simple products.

    Parameters
    ----------

    eval_seq
        The evaluation sequence whose FLOP cost is to be estimated.  It should
        be given as an iterable of tensor definitions.

    leading
        If only the cost terms with leading scaling be given.  When multiple
        symbols are present in the range sizes, terms with the highest total
        scaling is going to be picked.

    ignore_consts
        If the cost of scaling with constants can be ignored.  :math:`2 x_i y_j`
        could count as just one FLOP when it is set, otherwise it would be two.

    """

    cost = sum(_get_flop_cost(i, ignore_consts) for i in eval_seq)
    return _get_leading(cost) if leading else cost


def _get_flop_cost(step, ignore_consts):
    """Get the FLOP cost of a tensor evaluation step."""

    ext_size = get_total_size(step.exts)

    cost = Integer(0)
    n_terms = 0
    for term in step.rhs_terms:
        sum_size = get_total_size(term.sums)

        if isinstance(term.amp, Mul):
            factors = term.amp.args
        else:
            factors = (term.amp,)

        if ignore_consts:
            factors = (i for i in factors if not isinstance(i, Number))
        else:
            # Minus one should be implemented via subtraction, hence renders no
            # multiplication cost.
            factors = (i for i in factors if abs(i) != 1)

        n_factors = sum(1 for i in factors if abs(i) != 1)
        n_mult = n_factors - 1 if n_factors > 0 else 0

        if sum_size == 1:
            n_add = 0
        else:
            n_add = 1

        cost = add_costs(cost, (n_add + n_mult) * ext_size * sum_size)
        n_terms += 1
        continue

    if n_terms > 1:
        cost = add_costs(cost, (n_terms - 1) * ext_size)

    return cost


def _get_leading(cost):
    """Get the leading terms in a cost polynomial."""

    symbs = tuple(cost.atoms(Symbol))
    poly, _ = poly_from_expr(cost, *symbs)
    terms = poly.terms()

    leading_deg = max(sum(i) for i, _ in terms)
    leading_cost = sum(
        coeff * prod_(i ** j for i, j in zip(symbs, degs))
        for degs, coeff in terms
        if sum(degs) == leading_deg
    )

    return leading_cost


#
# Disjoint set forest.
#

class DSF(object):
    """
    Disjoint sets forest.

    This is a very simple implementation of the disjoint set forest data
    structure for finding the inseparable chunks of factors for given ranges to
    keep.  Heuristics of union by rank and path compression are both applied.

    Attributes
    ----------

    _contents
        The original contents of the nodes.  The object that was used for
        building the node.  Note that this class is designed with hashable and
        simple things like integers in mind.

    _parents
        The parent of the nodes.  Given as index in the contents list.

    _ranks
        The rank of the nodes.

    _locs
        The dictionary mapping the contents of the nodes into its location in
        this data structure.

    """

    def __init__(self, contents):
        """Initialize the object.

        Parameters
        ----------

        contents
            An iterable of the contents of the nodes.
        """

        self._contents = []
        self._ranks = []
        self._parents = []
        self._locs = {}

        for i, v in enumerate(contents):
            self._contents.append(v)
            self._ranks.append(0)
            self._parents.append(i)
            self._locs[v] = i
            continue

    def union(self, contents):
        """Put the given contents into union.

        Note that the nodes to be unioned are given in terms of their contents
        rather than their index. Also contents missing in the forest will just
        be **ignored**, which is what is needed for the case of inseparable
        chunk finding.

        Parameters
        ----------

        contents
            An iterable of the contents that are going to be put into the same
            set.

        """

        represent = None  # The set that other sets will be unioned to.
        for i in contents:

            try:
                loc = self._locs[i]
            except KeyError:
                continue

            if represent is None:
                represent = loc
            else:
                self._union_idxes(represent, loc)

            continue

        return None

    @property
    def sets(self):
        """The disjoints sets as actual sets.

        This property will convert the disjoint sets in the internal data
        structure into an actual list of sets.  A list of sets will be given,
        where each set contains the contents of the nodes in the subset.
        """

        # A dictionary with the set representative *index* as key and the
        # sets of the *contents* in the subset as values.
        sets_dict = collections.defaultdict(set)

        for i, v in enumerate(self._contents):
            sets_dict[self._find_set(i)].add(v)
            continue

        return list(sets_dict.values())

    def _union_idxes(self, idx1, idx2):
        """Union the subsets that the two given indices are in."""

        set1 = self._find_set(idx1)
        set2 = self._find_set(idx2)
        rank1 = self._ranks[set1]
        rank2 = self._ranks[set2]

        # Union by rank.
        if rank1 > rank2:
            self._parents[set2] = set1
        else:
            self._parents[set1] = set2
            if rank1 == rank2:
                self._ranks[set2] += 1

    def _find_set(self, idx):
        """Find the representative index of the subset the given index is in."""

        parent = self._parents[idx]
        if idx != parent:
            self._parents[idx] = self._find_set(parent)
        return self._parents[idx]


#
# Jinja environment creation
# --------------------------
#


def create_jinja_env(add_filters, add_globals, add_tests, add_templ):
    """Create a Jinja environment for template rendering.

    This function will create a Jinja environment suitable for rendering tensor
    expressions.  Notably the templates will be retrieved from the ``templates``
    directory in the package.  And some filters and predicates will be added,
    including

    wrap_line
    form_indent
    non_empty

    """

    # Set the Jinja environment up.
    env = Environment(
        trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
        loader=ChoiceLoader(
            [PackageLoader('gristmill')] +
            ([DictLoader(add_templ)] if add_templ is not None else [])
        )
    )

    # Add the default filters and tests for all printers.
    env.filters['wrap_line'] = wrap_line
    env.filters['form_indent'] = form_indent
    env.tests['non_empty'] = non_empty

    # Add the additional globals, filters, and tests.
    if add_globals is not None:
        env.globals.update(add_globals)
    if add_filters is not None:
        env.filters.update(add_filters)
    if add_tests is not None:
        env.tests.update(add_tests)

    return env


def wrap_line(line, breakable_regex, line_cont,
              base_indent=0, max_width=80, rewrap=False):
    """Wrap the given line within the given width.

    This function is going to be exported to be used by template writers in
    Jinja as a filter.

    Parameters
    ----------

    line
        The line to be wrapped.

    breakable_regex
        The regular expression giving the places where the line can be broke.
        The parts in the regular expression that needs to be kept can be put in
        a capturing parentheses.

    line_cont
        The string to be put by the end of line to indicate line continuation.

    base_indent
        The base indentation for the lines.

    max_width
        The maximum width of the lines to wrap the given line within.

    rewrap
        if the line is going to be rewrapped.

    Return
    ------
    A list of lines for the breaking of the given line.

    """

    # First compute the width that is available for actual content.
    avail_width = max_width - base_indent - len(line_cont)

    # Remove all the new lines and old line-continuation and indentation for
    # rewrapping.
    if rewrap:
        line = re.sub(
            line_cont + '\\s*\n\\s*', '', line
        )

    # Break the given line according to the given regular expression.
    trunks = re.split(breakable_regex, line)
    # Have a shallow check and issue warning.
    for i in trunks:
        if len(i) > avail_width:
            print('WARNING')
            print(
                'Trunk {} is longer than the given width of {}'.format(
                    i, max_width
                )
            )
            print('Longer width or finer partition can be given.')
        continue

    # Actually break the list of trunks into lines.
    lines = []
    curr_line = ''
    for trunk in trunks:

        if len(curr_line) == 0 or len(curr_line) + len(trunk) <= avail_width:
            # When we are able to add the trunk to the current line. Note that
            # when the current line is empty, the next trunk will be forced to
            # be added.
            curr_line += trunk
        else:
            # When the current line is already filled up.
            #
            # First dump the current line.
            lines.append(curr_line)
            # Then add the current trunk at the beginning of the next line. The
            # left spaces could be striped.
            curr_line = trunk.lstrip()

        # Go on to the next trunk.
        continue

    else:

        # We need to add the trailing current line after all the loop.
        lines.append(curr_line)

    # Before returning, we need to decorate the lines with indentation and
    # continuation suffix.
    decorated = [
        ''.join([
            ' ' * base_indent, v, line_cont if i != len(lines) - 1 else ''
        ])
        for i, v in enumerate(lines)
    ]
    return '\n'.join(decorated)


def non_empty(sequence):
    """Test if a given sequence is non-empty."""
    return len(sequence) > 0


@contextfilter
def form_indent(eval_ctx, num: int) -> str:
    """Form an indentation space block.

    Parameters
    ----------

    eval_ctx
        The evaluation context.
    num
        The number of the indentation. The size of the indentation is going to
        be read from the context by attribute ``indent_size``.

    Return
    ------

    A block of white spaces.

    """

    return ' ' * (
        eval_ctx['indent_size'] * (num + eval_ctx['global_indent'])
    )
