# Layer 3 build: C++20/POSIX, no cmake and no third-party libraries.
CXX      ?= g++
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra -Wpedantic -Iinclude -I../core_engine/include
LDFLAGS  ?=

SRC       := $(wildcard src/*.cpp)
OBJ       := $(SRC:.cpp=.o)
JSON_OBJ  := build/engine_json.o
LIB_OBJ   := $(filter-out src/main.o,$(OBJ)) $(JSON_OBJ)
TEST_SRC  := $(wildcard tests/test_*.cpp)
TEST_OBJ  := $(TEST_SRC:.cpp=.o)
TEST_BINS := $(TEST_SRC:.cpp=)
DEPS      := $(OBJ:.o=.d) $(TEST_OBJ:.o=.d) $(JSON_OBJ:.o=.d)

.PHONY: all tests test run clean

all: eva_bus

eva_bus: $(OBJ) $(JSON_OBJ)
	$(CXX) $(CXXFLAGS) -o $@ $(OBJ) $(JSON_OBJ) $(LDFLAGS)

$(JSON_OBJ): ../core_engine/src/json.cpp
	mkdir -p build
	$(CXX) $(CXXFLAGS) -MMD -MP -c -o $@ $<

%.o: %.cpp
	$(CXX) $(CXXFLAGS) -MMD -MP -c -o $@ $<

tests: eva_bus $(TEST_BINS)
	@set -e; for test_bin in $(TEST_BINS); do ./$$test_bin; done
	python3 -m unittest discover -s tests -p 'test_bus.py'

test: tests

tests/%: tests/%.o $(LIB_OBJ)
	$(CXX) $(CXXFLAGS) -o $@ $< $(LIB_OBJ) $(LDFLAGS)

.SECONDARY: $(TEST_OBJ)

run: eva_bus
	./eva_bus --dir ../data/processed --engine ../core_engine/eva_engine

clean:
	rm -f $(OBJ) $(JSON_OBJ) $(TEST_OBJ) $(DEPS) $(TEST_BINS) eva_bus tests/run_tests

-include $(DEPS)
