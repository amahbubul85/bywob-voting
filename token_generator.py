import csv, random, string, argparse

parser = argparse.ArgumentParser()
parser.add_argument("--count", type=int, default=10)
parser.add_argument("--prefix", type=str, default="BYWOB-2025")
args = parser.parse_args()

tokens = []
for _ in range(args.count):
    t = args.prefix + "-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    tokens.append([None,None,t,"FALSE",None])

with open("tokens.csv","w",newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["name","email","token","used","used_at"])
    writer.writerows(tokens)
print(f"{args.count} tokens written to tokens.csv")
