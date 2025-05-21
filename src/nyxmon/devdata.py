"""
Development data utilities for the nyxmon project.
This module contains functions for creating test or demo data.
"""

from nyxmon.domain.models import Result, ResultStatus


def create_test_results(num_recent=5, num_old=5, num_very_old=5):
    """
    Creates test Result objects with different creation times.

    Args:
        num_recent: Number of recent results to create (10 minutes ago)
        num_old: Number of old results to create (25 hours ago)
        num_very_old: Number of very old results to create (48 hours ago)

    Returns:
        Dictionary with categorized Result objects:
        {
            'recent': [Result objects],
            'old': [Result objects],
            'very_old': [Result objects],
            'all': [All Result objects]
        }
    """
    results = {"recent": [], "old": [], "very_old": [], "all": []}

    # Create recent results (would be from 10 minutes ago)
    for i in range(num_recent):
        result = Result(
            result_id=i + 1,
            check_id=101,
            status=ResultStatus.OK,
            data={"message": f"recent {i}"},
        )
        results["recent"].append(result)
        results["all"].append(result)

    # Create old results (would be from 25 hours ago)
    for i in range(num_old):
        result = Result(
            result_id=i + 101,
            check_id=102,
            status=ResultStatus.ERROR,
            data={"message": f"old {i}"},
        )
        results["old"].append(result)
        results["all"].append(result)

    # Create very old results (would be from 48 hours ago)
    for i in range(num_very_old):
        result = Result(
            result_id=i + 201,
            check_id=103,
            status=ResultStatus.OK,
            data={"message": f"very old {i}"},
        )
        results["very_old"].append(result)
        results["all"].append(result)

    return results
