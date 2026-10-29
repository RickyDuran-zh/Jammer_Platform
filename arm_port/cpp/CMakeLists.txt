cmake_minimum_required(VERSION 3.16)
project(jammer_rfio CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-Wall -Wextra -O2)

# ── locate UHD ────────────────────────────────────────────────────────────────
# Prefer pkg-config (UHD installs uhd.pc), fall back to a manual search so a
# source build in /usr/local works even when the CMake package files were not
# installed.  Override with:  cmake .. -DUHD_ROOT=/usr/local
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(PC_UHD QUIET uhd)
endif()

if(PC_UHD_FOUND)
  set(UHD_INCLUDE_DIRS ${PC_UHD_INCLUDE_DIRS})
  set(UHD_LIBRARIES ${PC_UHD_LIBRARIES})
  message(STATUS "jammer-rfio: UHD ${PC_UHD_VERSION} via pkg-config")
else()
  set(UHD_ROOT "/usr/local" CACHE PATH "UHD install prefix")
  find_path(UHD_INCLUDE_DIR uhd/usrp/multi_usrp.hpp
            HINTS ${UHD_ROOT}/include /usr/include /usr/local/include)
  find_library(UHD_LIBRARY uhd
               HINTS ${UHD_ROOT}/lib /usr/lib /usr/local/lib
                     /usr/lib/aarch64-linux-gnu /usr/local/lib/aarch64-linux-gnu)
  if(NOT UHD_INCLUDE_DIR OR NOT UHD_LIBRARY)
    message(FATAL_ERROR
      "UHD not found. Build it first (arm_port/scripts/build_uhd.sh) or pass "
      "-DUHD_ROOT=/path/to/uhd.  Looked for uhd/usrp/multi_usrp.hpp and libuhd.")
  endif()
  set(UHD_INCLUDE_DIRS ${UHD_INCLUDE_DIR})
  set(UHD_LIBRARIES ${UHD_LIBRARY})
  message(STATUS "jammer-rfio: UHD headers ${UHD_INCLUDE_DIR}, lib ${UHD_LIBRARY}")
endif()

find_package(Threads REQUIRED)

add_executable(jammer-rfio
  src/rfio_server.cpp
  src/rfio_radio.cpp
)
target_include_directories(jammer-rfio PRIVATE ${UHD_INCLUDE_DIRS})
target_link_libraries(jammer-rfio PRIVATE ${UHD_LIBRARIES} Threads::Threads)

install(TARGETS jammer-rfio RUNTIME DESTINATION bin)

# ── host-side tests (no UHD required to run, only to configure) ───────────────
option(JAMMER_RFIO_BUILD_TESTS "Build the rfio wire-format tests" ON)
if(JAMMER_RFIO_BUILD_TESTS)
  enable_testing()
  add_executable(test_json tests/test_json.cpp)
  target_include_directories(test_json PRIVATE include)
  add_test(NAME rfio_json COMMAND test_json)
endif()
