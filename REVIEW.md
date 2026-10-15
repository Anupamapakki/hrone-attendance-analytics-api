<!-- # REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Health endpoint| Returns success without verifying MongoDB connectivity. | | |

Also note anything you looked at and decided was **not** a defect, and why. -->

# REVIEW.md

## Defects Found in the Starter Implementation

| #   | Where (function / line)                | What is wrong                                                                                                        | How you'd notice it (test, input, or symptom)                                   | How you fixed it                                                                              |
| --- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 1   | Health endpoint                        | Returns success without verifying MongoDB connectivity.                                                              | Stop MongoDB and call the health endpoint; it may still return success.         | Added a database connectivity check and return HTTP 503 when unavailable.                     |
| 2   | `create_employee`                      | Duplicate checking before insertion is vulnerable to concurrent requests.                                            | Submit two requests with the same employee code simultaneously.                 | Added a unique index on `emp_code` and handled duplicate-key errors.                          |
| 3   | `create_employee`                      | Employee input validation and error handling are insufficient.                                                       | Submit malformed employee codes, invalid emails, or invalid shift times.        | Added field validation and appropriate error responses.                                       |
| 4   | `list_employees`                       | Pagination offset is calculated incorrectly when pages are one-based.                                                | Request page 1 and page 2 and check for overlapping or skipped records.         | Corrected the offset calculation and enforced pagination limits.                              |
| 5   | `list_employees`                       | Filtered results and total counts are not handled consistently; sorting is missing.                                  | Filter by department and compare the total count with the matching records.     | Applied filters consistently and added deterministic sorting and pagination.                  |
| 6   | Punch-in helper and endpoint           | Missing-employee handling and timestamp validation are insufficient.                                                 | Punch in for a nonexistent employee or submit an invalid timestamp.             | Added employee existence checks and timestamp validation.                                     |
| 7   | Punch-in helper                        | Overnight shifts can be assigned the wrong attendance date.                                                          | Test a shift from 22:00 to 06:00 with a punch after midnight.                   | Added overnight-shift handling when determining the attendance date.                          |
| 8   | Punch-in endpoint                      | A preliminary duplicate check alone cannot prevent concurrent duplicate attendance records.                          | Send simultaneous punch-in requests for the same employee and date.             | Added a unique compound index on `(emp_code, date)` and handled duplicate-key errors.         |
| 9   | `list_attendance`                      | Sorting and pagination are performed in Python rather than directly in MongoDB, and date validation is insufficient. | Test invalid date ranges and larger attendance datasets.                        | Added date validation and MongoDB-side filtering, sorting, counting, and pagination.          |
| 10  | Punch-out endpoint                     | The starter does not implement punch-out and the associated work-hour calculations.                                  | Call the required punch-out route or attempt to calculate completed work hours. | Implemented punch-out and calculations for work hours, overtime, and half-day status.         |
| 11  | Punch-out endpoint                     | Concurrent requests can attempt to close the same attendance record.                                                 | Send two punch-out requests for the same open record.                           | Added a conditional atomic update so an already-closed record cannot be closed again.         |
| 12  | Regularization endpoint                | Attendance correction, recalculation, and audit history are missing.                                                 | Submit a correction and check the updated values and change history.            | Implemented correction validation, recalculation of derived fields, and history tracking.     |
| 13  | Regularization helpers                 | Concurrent corrections can overwrite changes made by another request.                                                | Submit competing corrections for the same attendance record.                    | Added a concurrency check to detect conflicting updates.                                      |
| 14  | Monthly employee analytics             | The required monthly analytics endpoint is missing.                                                                  | Call the monthly analytics route and verify its required metrics.               | Added calculations for attendance, leave, late arrivals, overtime, and attendance percentage. |
| 15  | Department summary                     | The required department-level analytics are missing.                                                                 | Request a department summary and compare headcount and attendance metrics.      | Added MongoDB aggregation for headcount and department attendance metrics.                    |
| 16  | Late leaderboard                       | The required employee ranking by late minutes is missing.                                                            | Request the leaderboard and compare totals and ranks.                           | Added aggregation, ranking, and optional department filtering.                                |
| 17  | Department trend                       | The required daily department trend and moving average are missing.                                                  | Request a date range and inspect daily attendance rates and moving averages.    | Added a trend aggregation pipeline with daily metrics and a seven-day moving average.         |
| 18  | `/admin/explain/{endpoint}`            | The explain endpoint is missing from the starter.                                                                    | Call the required route for a supported endpoint.                               | Implemented explain output using MongoDB execution statistics.                                |
| 19  | Application startup and database setup | Required indexes are not explicitly created by the starter.                                                          | Inspect MongoDB indexes and test duplicate records or common filtered queries.  | Added unique indexes and supporting indexes for employee and attendance queries.              |

## Items Reviewed but Not Considered Defects

1. **MongoDB as the data store:** This is intentional because the assignment specifies MongoDB and defines the expected document structure.
2. **Using UTC timestamps:** UTC is appropriate for storing timestamps consistently. Indian Standard Time can be applied when interpreting attendance and shift times.
3. **Returning HTTP 409 for duplicate attendance:** This is appropriate when a conflicting request violates the one-record-per-employee-per-day rule.
4. **Using MongoDB aggregation for analytics:** This is appropriate because filtering and calculating metrics in the database avoids loading unnecessary records into application memory.
5. **Using unique indexes alongside validation:** These serve different purposes. Validation provides clear input errors, while unique indexes protect against duplicate writes under concurrent requests.

## Remaining Verification

The following items require targeted verification before claiming complete compliance:

- Ensure `compute_late_minutes()` and regularization use consistent rules for overnight shifts.
- Confirm department trend output includes exactly the requested date range.
- Confirm each explain branch uses the same filters, sorting, pagination, and pipeline as its corresponding endpoint.
- Verify `IXSCAN` usage and the absence of `COLLSCAN` on the required 100,000-record dataset.
- Compare all response fields, status codes, validation boundaries, and analytics calculations with the assignment contract.

## Conclusion

The updated implementation extends the starter with the required attendance operations, analytics endpoints, database safeguards, and explain functionality. The remaining verification focuses on edge cases, exact API-contract compliance, and query plans at the required dataset size.
