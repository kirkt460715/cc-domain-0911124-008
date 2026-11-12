"""Attribute acceptance sampling mathematics.

All probabilities use the finite-lot hypergeometric distribution (sampling
without replacement): a lot of size ``N`` containing ``D`` defectives yields

    P(X = x) = C(D, x) * C(N-D, n-x) / C(N, n)

for the number ``x`` of defectives found in a sample of size ``n``.  No
binomial or Poisson approximation is used, so small lots are modelled exactly.
The module is intentionally free of persistence concerns: the graph layer in
:mod:`chainledger.graph.sampling` freezes the plans returned here.
"""

from decimal import Decimal, ROUND_HALF_UP
from math import lgamma, exp

# Search bounds are deliberately explicit and queryable (see search_limits()).
SINGLE_SAMPLE_CAP = 2000
DOUBLE_FIRST_CAP = 125
DOUBLE_TOTAL_CAP = 250
RATE_DECIMALS = 9
RISK_EPSILON = 1e-12


def _ln_comb(a, b):
    if b < 0 or b > a:
        return float("-inf")
    if b == 0 or b == a:
        return 0.0
    return (
        lgamma(a + 1)
        - lgamma(b + 1)
        - lgamma(a - b + 1)
    )


def distribution(N, D, n):
    """Full hypergeometric probability mass function over x = 0..n."""
    low = max(0, n - (N - D))
    high = min(n, D)
    if low > high:
        return [0.0] * (n + 1)
    log_anchor = (
        _ln_comb(D, low)
        + _ln_comb(N - D, n - low)
        - _ln_comb(N, n)
    )
    mass = [0.0] * (n + 1)
    mass[low] = exp(log_anchor)
    for k in range(low, high):
        numerator = (D - k) * (n - k)
        denominator = (k + 1) * ((N - D) - (n - k) + 1)
        if denominator <= 0:
            break
        mass[k + 1] = mass[k] * numerator / denominator
    total = sum(mass)
    if 0.0 < total != 1.0:
        mass = [value / total for value in mass]
    return mass


def _head_cdf(N, D, n, up_to):
    """P(X <= up_to), evaluated without materialising the whole distribution."""
    if up_to < 0:
        return 0.0
    low = max(0, n - (N - D))
    high = min(n, D)
    if up_to >= high:
        # Every attainable outcome is accepted: X cannot exceed high.
        return 1.0
    if up_to < low:
        return 0.0
    log_anchor = (
        _ln_comb(D, low)
        + _ln_comb(N - D, n - low)
        - _ln_comb(N, n)
    )
    current = exp(log_anchor)
    total = current
    for k in range(low, up_to):
        numerator = (D - k) * (n - k)
        denominator = (k + 1) * ((N - D) - (n - k) + 1)
        if denominator <= 0:
            break
        current = current * numerator / denominator
        total += current
    return min(1.0, total)


def defect_count(N, rate):
    """Nearest-integer defective count for a lot fraction (half-up rounding)."""
    value = (
        Decimal(str(N))
        * Decimal(str(rate))
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(0, min(N, int(value)))


def _r9(value):
    return round(float(value), RATE_DECIMALS)


def single_plan_probabilities(N, n, accept):
    """Acceptance probability at every possible defective count."""
    mass = [0.0] * (N + 1)
    for D in range(N + 1):
        mass[D] = _head_cdf(N, D, n, accept)
    return mass


def evaluate_single(N, n, accept):
    if not 1 <= n <= N or not 0 <= accept < n:
        raise ValueError("invalid single sampling parameters")
    return {"n": n, "accept": accept, "curve": single_plan_probabilities(N, n, accept)}


def evaluate_double(N, n1, a1, r1, n2, a2):
    """OC curve and average sample number for a double sampling plan.

    First stage accepts when d1 <= a1, rejects when d1 >= r1 and continues in
    the interval a1 < d1 < r1.  The second stage accepts on cumulative defects
    d1 + d2 <= a2 and rejects otherwise.  Second-stage draws are taken from the
    remaining N-n1 units, so the same physical item can never be tested twice.
    """
    if not (1 <= n1 < N and 0 <= a1 < r1 and r1 >= a1 + 2):
        raise ValueError("invalid first-stage decision region")
    if not (1 <= n2 and n1 + n2 <= N and a1 < a2 < r1):
        raise ValueError("invalid second-stage parameters")
    accept_curve = [0.0] * (N + 1)
    asn_curve = [0.0] * (N + 1)
    for D in range(N + 1):
        first = distribution(N, D, n1)
        accept_first = sum(first[x] for x in range(a1 + 1))
        reject_first = sum(first[x] for x in range(r1, n1 + 1))
        continue_prob = 1.0 - accept_first - reject_first
        second_accept = 0.0
        for d1 in range(a1 + 1, r1):
            if first[d1] <= 0.0:
                continue
            remaining_defect = D - d1
            threshold = a2 - d1
            if remaining_defect <= 0 or threshold < 0:
                conditional = 1.0 if threshold >= 0 else 0.0
            else:
                conditional = _head_cdf(
                    N - n1, remaining_defect, n2, threshold
                )
            second_accept += first[d1] * conditional
        accept_curve[D] = accept_first + second_accept
        asn_curve[D] = n1 + n2 * continue_prob
    return {
        "n1": n1,
        "accept1": a1,
        "reject1": r1,
        "n2": n2,
        "accept2": a2,
        "curve": accept_curve,
        "asn_curve": asn_curve,
    }


def _risks_at(curve, d_aql, d_rql):
    # Producer risk: an AQL-quality lot is wrongly rejected.  Consumer risk:
    # an RQL-quality lot is wrongly accepted.  Both are probabilities of an
    # erroneous decision and are bounded by alpha and beta respectively.
    producer_risk = 1.0 - curve[d_aql]
    consumer_risk = curve[d_rql]
    return producer_risk, consumer_risk


def search_limits():
    """Queryable search caps and the documented preference order."""
    return {
        "single_sample_cap": SINGLE_SAMPLE_CAP,
        "double_first_sample_cap": DOUBLE_FIRST_CAP,
        "double_total_sample_cap": DOUBLE_TOTAL_CAP,
        "preference": [
            "single plans are preferred over double plans when kind=any",
            "single: smaller sample size n, then smaller acceptance number c",
            "double: smaller average sample size at AQL, then smaller n1, "
            "then smaller total sample n1+n2, then smaller acceptance numbers",
        ],
        "probability_model": "hypergeometric without replacement",
    }


def _oc_points(curve, N, aql, rql, grid=21):
    points = [0.0]
    for index in range(1, grid):
        points.append(index / grid)
    points.extend([aql, rql, 1.0])
    ordered = sorted(set(points))
    result = []
    for rate in ordered:
        D = defect_count(N, rate)
        result.append(
            {
                "defect_rate": _r9(rate),
                "defectives": D,
                "acceptance_probability": _r9(curve[D]),
            }
        )
    return result


def _asn_points(asn_curve, N, aql, rql, grid=21):
    if asn_curve is None:
        return None
    points = [0.0]
    for index in range(1, grid):
        points.append(index / grid)
    points.extend([aql, rql, 1.0])
    result = []
    for rate in sorted(set(points)):
        D = defect_count(N, rate)
        result.append(
            {
                "defect_rate": _r9(rate),
                "defectives": D,
                "average_sample_size": _r9(asn_curve[D]),
            }
        )
    return result


def design_sampling_plan(
    N,
    aql,
    rql,
    producer_risk,
    consumer_risk,
    kind="any",
):
    """Search for a single or double plan meeting both risk points.

    A feasible plan satisfies Pa(D0) >= 1 - alpha and Pa(D1) <= beta where
    D0/D1 are the defective counts nearest to AQL/RQL.  When nothing inside the
    queryable caps qualifies, an ``infeasible`` report is returned instead of a
    plan that violates the constraints.
    """
    if kind not in {"any", "single", "double"}:
        raise ValueError("kind must be any, single or double")
    d_aql = defect_count(N, aql)
    d_rql = defect_count(N, rql)
    target_producer = 1.0 - producer_risk
    context = {
        "lot_size": N,
        "aql": _r9(aql),
        "rql": _r9(rql),
        "producer_risk": _r9(producer_risk),
        "consumer_risk": _r9(consumer_risk),
        "aql_defectives": d_aql,
        "rql_defectives": d_rql,
        "kind": kind,
        "limits": search_limits(),
    }
    if d_aql >= d_rql:
        return _infeasible(
            context,
            "AQL and RQL round to the same defective count at this lot size",
        )
    best_single = None
    if kind in {"any", "single"}:
        best_single = _search_single(
            N, d_aql, d_rql, target_producer, consumer_risk
        )
    if kind == "single":
        return best_single or _infeasible(
            context, "no single plan within the sample size cap"
        )
    if best_single is not None and kind == "any":
        return best_single
    best_double = _search_double(
        N, d_aql, d_rql, target_producer, consumer_risk
    )
    if best_double is not None:
        return best_double
    return _infeasible(
        context,
        "no single or double plan within the sample size caps meets both risks",
    )


def _search_single(N, d_aql, d_rql, target_producer, consumer_risk):
    cap = min(SINGLE_SAMPLE_CAP, N)
    for n in range(1, cap + 1):
        # Build each hypergeometric distribution once per sample size and read
        # every acceptance number from its cumulative sum, keeping the search
        # linear in n rather than recomputing a CDF for each candidate.
        aql_mass = distribution(N, d_aql, n)
        rql_mass = distribution(N, d_rql, n)
        cumulative_aql = 0.0
        cumulative_rql = 0.0
        for accept in range(0, min(n, d_rql - 1) + 1):
            cumulative_aql += aql_mass[accept]
            cumulative_rql += rql_mass[accept]
            if cumulative_aql + RISK_EPSILON < target_producer:
                continue
            if cumulative_rql - RISK_EPSILON > consumer_risk:
                continue
            evaluation = evaluate_single(N, n, accept)
            producer, consumer = _risks_at(
                evaluation["curve"], d_aql, d_rql
            )
            # Smallest n wins, and within n the smallest c wins.
            return _package_single(
                N, n, accept, evaluation, producer, consumer,
                d_aql, d_rql,
            )
    # Full inspection (n=N, c=D0) separates the two defect counts exactly:
    # every AQL lot is accepted and every RQL lot rejected.  It is a legitimate
    # last resort only when the whole lot fits inside the sample cap.
    if N <= cap:
        evaluation = evaluate_single(N, N, d_aql)
        producer, consumer = _risks_at(evaluation["curve"], d_aql, d_rql)
        return _package_single(
            N, N, d_aql, evaluation, producer, consumer, d_aql, d_rql
        )
    return None


def _package_single(N, n, accept, evaluation, producer, consumer, d_aql, d_rql):
    return {
        "feasible": True,
        "kind": "single",
        "plan": {
            "sample_size": n,
            "acceptance_number": accept,
            "rejection_number": accept + 1,
        },
        "risks": {
            "producer_risk_at_aql": _r9(producer),
            "consumer_risk_at_rql": _r9(consumer),
        },
        "average_sample_size": {
            "at_aql": float(n),
            "at_rql": float(n),
            "curve": None,
        },
        "oc_curve": _oc_points(evaluation["curve"], N, d_aql / N, d_rql / N),
    }


def _search_double(N, d_aql, d_rql, target_producer, consumer_risk):
    best = None
    first_cap = min(DOUBLE_FIRST_CAP, N - 1)
    total_cap = min(DOUBLE_TOTAL_CAP, N)
    for n1 in range(1, first_cap + 1):
        first_aql = distribution(N, d_aql, n1)
        first_rql = distribution(N, d_rql, n1)
        n2_cap = total_cap - n1
        if n2_cap < 1:
            break
        for a1 in range(0, n1):
            # Total RQL acceptance can never be below the first-stage
            # acceptance probability.  Once even that exceeds beta, no second
            # stage can rescue this or any larger a1.
            rql_immediate_accept = sum(first_rql[: a1 + 1])
            if rql_immediate_accept > consumer_risk + RISK_EPSILON:
                break
            for r1 in range(a1 + 2, n1 + 2):
                candidate = _best_second_stage(
                    N, n1, a1, r1, n2_cap, d_aql, d_rql,
                    target_producer, consumer_risk,
                    first_aql, first_rql,
                )
                if candidate is None:
                    continue
                if best is None or candidate["_order"] < best["_order"]:
                    best = candidate
                # r1 cannot be pruned early: a smaller rejection bound pushes
                # more lots into the second stage and can raise the ASN, so
                # every decision region must be compared on its ASN.
        if best is not None:
            # Preference orders by ASN then n1, so the first n1 with a feasible
            # plan dominates every larger n1.
            break
    if best is not None:
        best.pop("_order", None)
    return best


def _best_second_stage(
    N, n1, a1, r1, n2_cap, d_aql, d_rql,
    target_producer, consumer_risk, first_aql, first_rql,
):
    """Smallest-ASN second stage for one first-stage decision region.

    With the cumulative acceptance number fixed, both Pa at AQL and Pa at RQL
    are non-increasing in the second sample size n2.  The feasible values
    therefore form an interval [n2_low, n2_high]: below n2_low the RQL is not
    rejected often enough, above n2_high the AQL is not accepted often enough.
    Both edges are located with binary search, so the smallest feasible n2 -
    and hence the smallest average sample size - is found in logarithmic time.
    """
    chosen = None
    for a2 in range(a1 + 1, r1):
        minimal_n2 = _minimal_second_sample(
            N, n1, a1, r1, a2, n2_cap, d_aql, d_rql,
            target_producer, consumer_risk, first_aql, first_rql,
        )
        if minimal_n2 is None:
            continue
        evaluation = evaluate_double(N, n1, a1, r1, minimal_n2, a2)
        producer, consumer = _risks_at(
            evaluation["curve"], d_aql, d_rql
        )
        packaged = _package_double(
            N, evaluation, producer, consumer, d_aql, d_rql
        )
        order = (
            packaged["average_sample_size"]["at_aql"],
            n1,
            n1 + minimal_n2,
            a1,
            a2,
        )
        if chosen is None or order < chosen["_order"]:
            packaged["_order"] = order
            chosen = packaged
    return chosen


def _minimal_second_sample(
    N, n1, a1, r1, a2, n2_cap, d_aql, d_rql,
    target_producer, consumer_risk, first_aql, first_rql,
):
    def pa_aql(n2):
        return _double_accept(
            N, d_aql, n1, a1, r1, n2, a2, first_aql
        )

    def pa_rql(n2):
        return _double_accept(
            N, d_rql, n1, a1, r1, n2, a2, first_rql
        )

    # The smallest n2 must already satisfy the consumer condition; if the
    # largest allowed n2 fails it, no feasible value exists in the interval.
    if pa_rql(n2_cap) - RISK_EPSILON > consumer_risk:
        return None
    # Producer condition: Pa_AQL is non-increasing, so find the largest n2 that
    # still meets the producer target (n2_high).
    if pa_aql(n2_cap) + RISK_EPSILON >= target_producer:
        n2_high = n2_cap
    else:
        low, high = 1, n2_cap
        while low < high:
            mid = (low + high + 1) // 2
            if pa_aql(mid) + RISK_EPSILON >= target_producer:
                low = mid
            else:
                high = mid - 1
        n2_high = low
    # Consumer condition: Pa_RQL is non-increasing, so find the smallest n2
    # that meets the consumer target (n2_low).
    if pa_rql(1) - RISK_EPSILON <= consumer_risk:
        n2_low = 1
    else:
        low, high = 1, n2_cap
        while low < high:
            mid = (low + high) // 2
            if pa_rql(mid) - RISK_EPSILON <= consumer_risk:
                high = mid
            else:
                low = mid + 1
        n2_low = low
    if n2_low > n2_high:
        return None
    return n2_low


def _double_accept(N, D, n1, a1, r1, n2, a2, first_mass):
    accept_first = sum(first_mass[x] for x in range(a1 + 1))
    second_accept = 0.0
    for d1 in range(a1 + 1, r1):
        if first_mass[d1] <= 0.0:
            continue
        remaining_defect = D - d1
        threshold = a2 - d1
        if remaining_defect <= 0:
            conditional = 1.0 if threshold >= 0 else 0.0
        else:
            conditional = _head_cdf(N - n1, remaining_defect, n2, threshold)
        second_accept += first_mass[d1] * conditional
    return accept_first + second_accept


def _package_double(N, evaluation, producer, consumer, d_aql, d_rql):
    aql_rate = d_aql / N
    rql_rate = d_rql / N
    return {
        "feasible": True,
        "kind": "double",
        "plan": {
            "first_sample_size": evaluation["n1"],
            "first_accept": evaluation["accept1"],
            "first_reject": evaluation["reject1"],
            "second_sample_size": evaluation["n2"],
            "cumulative_accept": evaluation["accept2"],
            "cumulative_reject": evaluation["accept2"] + 1,
            "maximum_sample_size": evaluation["n1"] + evaluation["n2"],
        },
        "risks": {
            "producer_risk_at_aql": _r9(producer),
            "consumer_risk_at_rql": _r9(consumer),
        },
        "average_sample_size": {
            "at_aql": _r9(evaluation["asn_curve"][d_aql]),
            "at_rql": _r9(evaluation["asn_curve"][d_rql]),
            "curve": _asn_points(
                evaluation["asn_curve"], N, aql_rate, rql_rate
            ),
        },
        "oc_curve": _oc_points(
            evaluation["curve"], N, aql_rate, rql_rate
        ),
    }


def _infeasible(context, reason):
    return {
        "feasible": False,
        "reason": reason,
        "design": context,
    }
