redo-ifchange _all
redo minimal/test docs/test t/all
[ -n "$DO_BUILT" ] || echo "Don't forget to test 'minimal/do -c test'" >&2
