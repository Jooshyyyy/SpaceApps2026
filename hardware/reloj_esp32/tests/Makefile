CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -O2

.PHONY: test

# Scratch build lives outside the worktree and is removed even on failure.
test:
	@tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' EXIT INT TERM; \
	$(CXX) $(CXXFLAGS) test_watch_transport.cpp -o "$$tmp/test_watch_transport" && \
	"$$tmp/test_watch_transport" && \
	$(CXX) $(CXXFLAGS) api_probe.cpp -o "$$tmp/api_probe" && \
	"$$tmp/api_probe"
