// SPDX-License-Identifier: Apache-2.0
#include "rig_publication_provenance.hpp"
#include <cassert>
#include <dlfcn.h>
#include <iostream>

using namespace rig_publication_provenance;
using Data = std::shared_ptr<rcutils_uint8_array_t>;

#ifdef RIG_PRODUCER
extern "C" __attribute__((visibility("default"))) Data make_envelope(int * destroyed)
{
  auto data = Data(new rcutils_uint8_array_t{}, [destroyed](auto * value) {
      ++*destroyed;
      delete value;
    });
  uint8_t gid[16] = {1};
  attach(data, publication_metadata("test:scope", "rmw_zenoh_cpp", 42,
    gid, 16, UINT64_C(0x12345678ffffffff)));
  return data;
}
#else
int main(int argc, char ** argv)
{
  assert(argc == 2);
  uint32_t allocated = UINT16_MAX - 1;
  require_channel_capacity(allocated);
  ++allocated;  // Allocation of channel 65535 succeeds; channel 65536 must not occur.
  bool rejected = false;
  try {require_channel_capacity(allocated); ++allocated;}
  catch (const std::runtime_error & error) {
    rejected = std::string(error.what()).find("split the bag") != std::string::npos;
  }
  assert(rejected && allocated == UINT16_MAX);
  rejected = false;
  try {require_channel_capacity(UINT32_MAX);}
  catch (const std::runtime_error &) {rejected = true;}
  assert(rejected);
  uint8_t gid[16] = {1};
  assert(safe_scope("boot:11111111-1111-1111-1111-111111111111"));
  assert(!safe_scope(""));
  assert(!safe_scope("bad scope"));
  assert(!safe_scope(std::string(129, 'x')));
  assert(publication_metadata("scope", "rmw_fastrtps_cpp", 0, gid, 16, 1).empty());
  assert(publication_metadata("scope", "rmw_zenoh_cpp", 0, gid, 15, 1).empty());
  assert(publication_metadata("scope", "rmw_zenoh_cpp", 0, gid, 16, UINT64_MAX).empty());
  uint8_t zero[16] = {};
  assert(publication_metadata("scope", "rmw_zenoh_cpp", 0, zero, 16, 1).empty());
  auto low = publication_metadata("scope", "rmw_zenoh_cpp", 0, gid, 16, UINT32_MAX);
  auto high = publication_metadata("scope", "rmw_zenoh_cpp", 0, gid, 16, UINT64_C(1) << 32);
  assert(low.at("rig.provenance.sequence_upper32") == "0");
  assert(high.at("rig.provenance.sequence_upper32") == "1");
  assert(channel_key("/topic", low) != channel_key("/topic", high));
  assert(channel_key("/other", low) != channel_key("/topic", low));
  assert(metadata(std::make_shared<rcutils_uint8_array_t>()) == nullptr);
  CaptureCache cache("scope", "rmw_zenoh_cpp", 0);
  auto first = cache.get(gid, 16, 1);
  assert(first == cache.get(gid, 16, 2));
  assert(first != cache.get(gid, 16, UINT64_C(1) << 32));
  assert(!cache.get(zero, 16, 1));
  assert(!cache.get(gid, 16, UINT64_MAX));
  for (uint64_t high32 = 2; high32 < 132; ++high32) {
    assert(cache.get(gid, 16, high32 << 32));
  }
  assert(first->fields.at("rig.provenance.sequence_upper32") == "0");
  assert(first != cache.get(gid, 16, 3));  // Evicted cache entry remains alive in queued owner.

  // RTLD_LOCAL and hidden compilation exercise cross-DSO RTTI, not a single TU shortcut.
  auto * library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
  if (!library) {std::cerr << dlerror() << std::endl; return 1;}
  auto make = reinterpret_cast<Data (*)(int *)>(dlsym(library, "make_envelope"));
  assert(make);
  int destroyed = 0;
  {
    auto data = make(&destroyed);
    auto copy = data;
    data.reset();
    assert(destroyed == 0);
    const auto * identity = metadata(copy);
    assert(identity);
    assert(identity->at("rig.provenance.writer_gid") == "01000000000000000000000000000000");
    assert(identity->at("rig.provenance.sequence_upper32") == "305419896");
    assert(identity->at("rig.provenance.domain_id") == "42");
  }
  assert(destroyed == 1);
  dlclose(library);
  std::cout << "provenance ownership and sequence tests passed" << std::endl;
}
#endif
