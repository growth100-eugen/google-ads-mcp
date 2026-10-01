from pathlib import Path
p = Path('/usr/local/lib/python3.11/site-packages/fastmcp/server/auth/oauth_proxy/proxy.py')
old = 'metadata.authorization_response_iss_parameter_supported = True'
new = 'metadata.authorization_response_iss_parameter_supported = False'
s = p.read_text()
if old in s:
    p.write_text(s.replace(old, new))
    print('fastmcp patched')
else:
    print('patch not needed')
