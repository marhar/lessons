import duckdb

con = duckdb.connect()

sql = open("both.sql").read()
tbl = con.sql(sql).to_df()

print(tbl)
