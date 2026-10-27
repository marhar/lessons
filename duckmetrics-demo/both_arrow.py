import duckdb

con = duckdb.connect()

sql = open("both.sql").read()
tbl = con.sql(sql).to_arrow_table()

print(tbl)
