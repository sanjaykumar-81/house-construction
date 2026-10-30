-- DAY 13: CASE STATEMENT


-- 1. Categorize employees by salary

SELECT
    name,
    salary,
    CASE
        WHEN salary >= 65000 THEN 'High Salary'
        WHEN salary >= 55000 THEN 'Medium Salary'
        ELSE 'Low Salary'
    END AS salary_category
FROM employees;


-- 2. Categorize employees by department

SELECT
    name,
    department,
    CASE
        WHEN department = 'IT' THEN 'Technology'
        WHEN department = 'HR' THEN 'Human Resources'
        WHEN department = 'Finance' THEN 'Finance Department'
        ELSE 'Other'
    END AS department_type
FROM employees;


-- 3. Create a bonus based on salary

SELECT
    name,
    salary,
    CASE
        WHEN salary >= 70000 THEN salary * 0.15
        WHEN salary >= 60000 THEN salary * 0.10
        ELSE salary * 0.05
    END AS bonus
FROM employees;


-- 4. Calculate salary after bonus

SELECT
    name,
    salary,
    salary +
    CASE
        WHEN salary >= 70000 THEN salary * 0.15
        WHEN salary >= 60000 THEN salary * 0.10
        ELSE salary * 0.05
    END AS salary_after_bonus
FROM employees;


-- 5. Mark employees as Senior or Junior

SELECT
    name,
    salary,
    CASE
        WHEN salary >= 65000 THEN 'Senior'
        ELSE 'Junior'
    END AS employee_level
FROM employees;


-- 6. Count employees in each salary category

SELECT
    CASE
        WHEN salary >= 65000 THEN 'High'
        WHEN salary >= 55000 THEN 'Medium'
        ELSE 'Low'
    END AS salary_category,
    COUNT(*) AS employee_count
FROM employees
GROUP BY
    CASE
        WHEN salary >= 65000 THEN 'High'
        WHEN salary >= 55000 THEN 'Medium'
        ELSE 'Low'
    END;


-- 7. Sort employees by salary category

SELECT
    name,
    salary,
    CASE
        WHEN salary >= 65000 THEN 'High'
        WHEN salary >= 55000 THEN 'Medium'
        ELSE 'Low'
    END AS salary_category
FROM employees
ORDER BY
    CASE
        WHEN salary >= 65000 THEN 1
        WHEN salary >= 55000 THEN 2
        ELSE 3
    END;
