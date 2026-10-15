# DECISIONS.md (about 150-200 words in total - plain language, your own words)

### 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes and a unique compound index on `attendance_logs.emp_code` and `date` to prevent duplicate daily attendance records. Supporting indexes include `employees.joined_on`, `(employees.department, joined_on)`, `(employees.department, emp_code)`, `(attendance_logs.date DESC, emp_code ASC)`, `(attendance_logs.date, status, emp_code)`, `(attendance_logs.emp_code, punch_in DESC)`, and `(attendance_logs.date, late_minutes, emp_code)`. These support employee filtering, attendance sorting, recent punch-in lookups, and late-minute queries. I would reject an additional index unless query plans and benchmarks showed a clear benefit.

### 2. Punch-in race

Both requests may pass the initial duplicate check. MongoDB's unique compound index allows only one insert. The other raises a duplicate-key error, which the API converts to HTTP 409 Conflict.

### 3. Ties

The leaderboard uses competition ranking. Employees tied at the cutoff are all returned if their rank is within the limit, so the response can contain more records than the requested limit.

### 4. Headcount

The department summary starts from eligible employees and looks up attendance logs. Employees without logs remain in the headcount.

### 5. Scaling

For 100 times the data, I would benchmark aggregation pipelines and inspect execution plans. If necessary, I would introduce daily pre-aggregated metrics to reduce repeated calculations.
