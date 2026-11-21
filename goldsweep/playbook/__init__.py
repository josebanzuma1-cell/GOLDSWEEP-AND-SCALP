"""Session playbooks (Phase 10, D86): ``python -m goldsweep.playbook``.

For each trading session (Asian/Tokyo, London, New York) and each setup model,
a small fixed grid of A+ rules is traded on the stored history; the best rule on
the first two-thirds of the period becomes the session's candidate only if it is
also positive on the last third, which it never saw.
"""
