# Layer 2 build. cmake is not installed, so this stays a plain Makefile.
CXX      ?= g++
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra -Wpedantic -Iinclude
LDFLAGS  ?=

SRC      := $(wildcard src/*.cpp)
OBJ      := $(SRC:.cpp=.o)
LIB_OBJ  := $(filter-out src/main.o,$(OBJ))
TEST_OBJ := tests/test_engine.o
DEPS     := $(OBJ:.o=.d) $(TEST_OBJ:.o=.d)

.PHONY: all tests test run clean

all: eva_engine

eva_engine: $(OBJ)
	$(CXX) $(CXXFLAGS) -o $@ $(OBJ) $(LDFLAGS)

%.o: %.cpp
	$(CXX) $(CXXFLAGS) -MMD -MP -c -o $@ $<

tests: eva_engine tests/run_tests
	./tests/run_tests
	python3 -m unittest discover -s tests -p 'test_cli.py'

test: tests

tests/run_tests: $(TEST_OBJ) $(LIB_OBJ)
	$(CXX) $(CXXFLAGS) -o $@ $(TEST_OBJ) $(LIB_OBJ) $(LDFLAGS)

run: eva_engine
	./eva_engine info --dir ../data/processed

clean:
	rm -f $(OBJ) $(TEST_OBJ) $(DEPS) eva_engine tests/run_tests

-include $(DEPS)
