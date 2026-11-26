# Codespace management targets (D31). Included by the Makefile in both modes, so they always
# run on the laptop. `make remote-on` makes every OTHER target run in the codespace instead
# (scripts/remote.mk); `make remote-off` goes back to running everything locally.

REMOTE_STATE_FILE := .inframind-remote.mk
CODESPACE_SH      := bash scripts/codespace.sh

.PHONY: remote-on remote-off remote-status codespace-create codespace-status codespace-stop \
	codespace-delete codespace-ssh codespace-ports codespace-logs codespace-fetch

remote-on: ## run every make target in the GitHub codespace from now on (D31)
	@echo 'INFRAMIND_REMOTE := codespace' > $(REMOTE_STATE_FILE)
	@echo "remote mode ON: make <target> now runs in the '$${CODESPACE_DISPLAY_NAME:-inframind-testbed}' codespace (created on first use)."
	@echo "it runs the pushed branch; 'make down' also stops the codespace; 'make remote-off' to undo."

remote-off: ## run make targets on this machine again
	@rm -f $(REMOTE_STATE_FILE)
	@echo "remote mode OFF: make targets run locally."

remote-status: ## show whether make targets run locally or in the codespace
	@if [ -f $(REMOTE_STATE_FILE) ]; then echo "remote mode: ON (codespace)"; else echo "remote mode: OFF (local)"; fi
	@$(CODESPACE_SH) status

codespace-create: ## create the testbed codespace for the current branch (4 cores / 16 GB)
	@$(CODESPACE_SH) create

codespace-status: ## show the testbed codespace and its state
	@$(CODESPACE_SH) status

codespace-stop: ## stop the codespace without touching the testbed inside it
	@$(CODESPACE_SH) stop

codespace-delete: ## delete the codespace and everything in it (recordings not fetched are lost)
	@$(CODESPACE_SH) delete

codespace-ssh: ## open a shell in the codespace
	@$(CODESPACE_SH) ssh

codespace-ports: ## forward Grafana/Prometheus/Alertmanager/Jaeger/Loki/API to localhost (Ctrl+C stops)
	@$(CODESPACE_SH) ports

codespace-logs: ## follow the latest remote eval-live recording log
	@$(CODESPACE_SH) logs

codespace-fetch: ## copy evaluation/fixtures and evaluation/results from the codespace to here
	@$(CODESPACE_SH) fetch
