"""One number, in one place.

It travels with every request to the control plane, which is the only lever
that exists once this package is running on somebody else's server. A bug
fixed here cannot be pushed to them; the control plane can only refuse to
answer a version it knows is broken, and let the merchant see why.
"""

__version__ = "1.2.0"

# Sent as a header on every call so the control plane can refuse a release it
# has since found a fault in.
USER_AGENT = f"commerce-policy/{__version__}"
