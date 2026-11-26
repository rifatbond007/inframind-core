# Remote mode (D31): included instead of the normal Makefile body when `make remote-on` is
# set and we are NOT already inside a codespace. Every goal on the command line, except the
# codespace management targets in scripts/codespace.mk, is forwarded as ONE `make` call to
# the codespace, with the command-line variables (EVAL_SET=..., SCENARIO=...).
#   make up                              -> make up                (in the codespace)
#   make eval-live EVAL_SET=test SCENARIO=x  -> detached recording  (in the codespace)
#   make down                            -> make down, then the codespace is stopped

REMOTE_LOCAL_GOALS := remote-on remote-off remote-status codespace-%
_REMOTE_GOALS      := $(or $(MAKECMDGOALS),help)
FORWARD_GOALS      := $(filter-out $(REMOTE_LOCAL_GOALS),$(_REMOTE_GOALS))
FORWARD_VARS       := $(filter-out INFRAMIND_REMOTE=%,$(-*-command-variables-*-))

ifneq ($(FORWARD_GOALS),)
_FIRST_GOAL := $(firstword $(FORWARD_GOALS))
.DEFAULT_GOAL := $(_FIRST_GOAL)
.PHONY: $(FORWARD_GOALS)

$(_FIRST_GOAL):
	@bash scripts/codespace.sh run $(FORWARD_GOALS) $(FORWARD_VARS)

# The other goals were forwarded together with the first one.
$(filter-out $(_FIRST_GOAL),$(FORWARD_GOALS)): $(_FIRST_GOAL) ; @:
endif
