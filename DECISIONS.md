# DECISIONS

1. **Indexes.** Employees: unique `emp_code`, `(department, emp_code)`, `joined_on`. Attendance: unique `(emp_code, date)`, `(date desc, emp_code)`, `(status, date desc, emp_code)`, `(emp_code, status, date desc)`, `(emp_code, punch_in desc)`. Each one serves a filter plus its sort order, or a uniqueness guarantee. I rejected a lone `status` index: few distinct values, and it cannot give the sort.

2. **Identical punch-ins.** Both requests try to insert. The unique `(emp_code, date)` index lets one succeed (201) and rejects the other with a duplicate-key error, which becomes 409. I never check first whether a record exists.

3. **Tie at the cutoff.** Ranking is 1, 2, 2, 4, and `limit` is applied to the rank, not the row count. Everyone tied at the cutoff is returned, so there can be more rows than `limit`.

4. **No-log employees.** The summary starts from the employees collection (joined on or before month end) and attaches logs afterwards, keeping people with none. Headcount counts employees, not log rows.

5. **100x data.** I would keep pre-computed daily totals per employee, so analytics read small summary rows instead of scanning raw logs.
