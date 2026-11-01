-- DAY 14: STRING FUNCTIONS


-- 1. Convert names to uppercase

SELECT
    name,
    UPPER(name) AS uppercase_name
FROM employees;


-- 2. Convert names to lowercase

SELECT
    name,
    LOWER(name) AS lowercase_name
FROM employees;


-- 3. Find the length of each employee's name

SELECT
    name,
    LENGTH(name) AS name_length
FROM employees;


-- 4. Combine employee name and department

SELECT
    name,
    department,
    CONCAT(name, ' - ', department) AS employee_info
FROM employees;


-- 5. Get the first 3 characters of each name

SELECT
    name,
    SUBSTRING(name, 1, 3) AS short_name
FROM employees;


-- 6. Combine multiple string functions

SELECT
    name,
    UPPER(name) AS uppercase_name,
    LOWER(name) AS lowercase_name,
    LENGTH(name) AS name_length,
    SUBSTRING(name, 1, 3) AS short_name
FROM employees;


-- 7. Create a professional employee label

SELECT
    CONCAT(
        UPPER(name),
        ' - ',
        UPPER(department)
    ) AS employee_label
FROM employees;


-- 8. Find employees whose names have more than 5 characters

SELECT
    name,
    LENGTH(name) AS name_length
FROM employees
WHERE LENGTH(name) > 5;
