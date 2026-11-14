"""The ARCADE welcome gift (chains.welcome_arcade): once per account, owed and tried again when the arcade had none."""
import sys, time
sys.path[:0] = ["arcade-src", "."]
from arcadesite import chains as chainslib
from arcadesite.v1 import V1Error

class St:
    def __init__(self): self.s = {}
    def setting(self, k, d=None): return self.s.get(k, d)
    def set_setting(self, k, v): self.s[k] = v

class V1:
    def __init__(self): self.sent, self.fail = [], False
    def cmd(self, name, **kw):
        if name == "chains.list": return [{"key": "arctest", "installed": True, "indexes": ["addresses"]}]
        if name == "wallet.send":
            if self.fail: raise V1Error("no spendable coins yet")
            self.sent.append(kw); return {"txid": "ab" * 32}
        raise V1Error(name)

st, v1 = St(), V1()
c = chainslib.Chains(st, v1)
pk = "11" * 32
st.set_setting(f"address:{pk}", "nVrY9PSLXmV5LP6NN5h4XjqfDNu8uq24N2")
assert c.arcade_address(pk) == "aRiSc8VhEManoNumdVhVyyg4LFAqaCKQya", c.arcade_address(pk)
assert c.welcome_arcade(pk) == "", "nothing owed to an account that never signed up here"
v1.fail = True
try:
    c.welcome_arcade(pk, new=True); raise SystemExit("FAIL: should have raised")
except V1Error:
    pass
assert st.setting(f"arcade_gift_owed:{pk}") is not None and not st.setting(f"arcade_gift:{pk}")
v1.fail = False
assert c.welcome_arcade(pk) == "", "not tried again so soon"
st.set_setting(f"arcade_gift_owed:{pk}", time.time() - 700)
assert c.welcome_arcade(pk) == "ab" * 32
assert v1.sent == [{"chain": "arctest", "to": "aRiSc8VhEManoNumdVhVyyg4LFAqaCKQya", "amount": 10_000_000, "send": True}]
st.set_setting(f"arcade_gift_owed:{pk}", 0)
assert c.welcome_arcade(pk, new=True) == "" and len(v1.sent) == 1, "never twice"
print("ALL OK")
