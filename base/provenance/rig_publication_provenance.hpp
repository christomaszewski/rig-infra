// Copyright 2026 Rig contributors. SPDX-License-Identifier: Apache-2.0
// Private, header-only envelope shared by the patched DSOs. Public ROS ABI is unchanged.
#ifndef RIG_PUBLICATION_PROVENANCE_HPP_
#define RIG_PUBLICATION_PROVENANCE_HPP_

#include <cstdint>
#include <array>
#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include "rcutils/types/uint8_array.h"

namespace rig_publication_provenance
{
using Metadata = std::map<std::string, std::string>;
constexpr const char * marker = "rig.provenance.schema";

inline void require_channel_capacity(uint32_t allocated)
{
  // MCAP assigns uint16_t(channels.size() + 1) without an overflow check. ID zero
  // must not wrap and alias an earlier publisher's channel, even for topic-only bags.
  if (allocated >= UINT16_MAX) {
    throw std::runtime_error(
      "MCAP channel limit reached (65535); split the bag before adding another topic or publication identity");
  }
}

struct Identity
{
  Metadata fields;
  std::string key;
};
using IdentityPtr = std::shared_ptr<const Identity>;

inline IdentityPtr identity(Metadata fields)
{
  std::string key;
  for (const auto & pair : fields) {
    key += std::to_string(pair.first.size()) + ":" + pair.first;
    key += std::to_string(pair.second.size()) + ":" + pair.second;
  }
  return std::make_shared<const Identity>(Identity{std::move(fields), std::move(key)});
}

// A deleter is part of shared_ptr's ownership block: it follows cache copies without an
// address-indexed side table, and dies with exactly this payload. Default visibility makes
// its RTTI identifiable via get_deleter across the recorder and storage plugin DSOs.
struct __attribute__((visibility("default"))) Owner
{
  std::shared_ptr<rcutils_uint8_array_t> data;
  IdentityPtr identity;
  void operator()(rcutils_uint8_array_t *) const noexcept {}
};

inline void attach(std::shared_ptr<rcutils_uint8_array_t> & data, IdentityPtr identity)
{
  auto * pointer = data.get();
  data = std::shared_ptr<rcutils_uint8_array_t>(pointer, Owner{data, std::move(identity)});
}

inline void attach(std::shared_ptr<rcutils_uint8_array_t> & data, Metadata fields)
{
  attach(data, identity(std::move(fields)));
}

inline const Identity * envelope(const std::shared_ptr<rcutils_uint8_array_t> & data)
{
  auto * owner = std::get_deleter<Owner>(data);
  return owner ? owner->identity.get() : nullptr;
}

inline IdentityPtr identity_owner(const std::shared_ptr<rcutils_uint8_array_t> & data)
{
  auto * owner = std::get_deleter<Owner>(data);
  return owner ? owner->identity : nullptr;
}

inline const Metadata * metadata(const std::shared_ptr<rcutils_uint8_array_t> & data)
{
  auto * value = envelope(data);
  return value ? &value->fields : nullptr;
}

inline bool safe_scope(const std::string & value)
{
  if (value.empty() || value.size() > 128) {return false;}
  for (unsigned char c : value) {
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
      (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.' || c == ':'))
    {return false;}
  }
  return true;
}

inline std::string recording_scope()
{
  if (const char * value = std::getenv("RIG_PUBLICATION_SCOPE")) {
    if (*value) {return safe_scope(value) ? value : "";}
  }
  std::ifstream file("/proc/sys/kernel/random/boot_id");
  std::string boot;
  std::getline(file, boot);
  if (boot.size() != 36) {return "";}
  for (size_t i = 0; i < boot.size(); ++i) {
    const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
    if (dash ? boot[i] != '-' : !((boot[i] >= '0' && boot[i] <= '9') ||
      (boot[i] >= 'a' && boot[i] <= 'f'))) {return "";}
  }
  return "boot:" + boot;
}

inline Metadata publication_metadata(
  const std::string & scope, const std::string & rmw, size_t domain,
  const uint8_t * gid, size_t gid_size, uint64_t sequence)
{
  if (!safe_scope(scope) || rmw != "rmw_zenoh_cpp" || gid_size != 16 ||
    sequence == UINT64_MAX) {return {};}
  bool nonzero = false;
  std::string hex;
  for (size_t i = 0; i < gid_size; ++i) {
    nonzero |= gid[i] != 0;
    hex += "0123456789abcdef"[gid[i] >> 4];
    hex += "0123456789abcdef"[gid[i] & 15];
  }
  if (!nonzero) {return {};}
  return {{marker, "1"}, {"rig.provenance.kind", "rmw_publication"},
    {"rig.provenance.scope", scope}, {"rig.provenance.rmw", rmw},
    {"rig.provenance.domain_id", std::to_string(domain)},
    {"rig.provenance.writer_gid", hex},
    {"rig.provenance.sequence_upper32", std::to_string(sequence >> 32)}};
}

// Length-prefixing all keys and values avoids delimiter collisions and separates topics,
// writers, upper32 epochs, and unknown/unmarked messages without changing topic names.
inline std::string channel_key(const std::string & topic, const Metadata & metadata)
{
  std::string key = std::to_string(topic.size()) + ":" + topic;
  for (const auto & pair : metadata) {
    key += std::to_string(pair.first.size()) + ":" + pair.first;
    key += std::to_string(pair.second.size()) + ":" + pair.second;
  }
  return key;
}

// At most 128 recently seen writer/upper32 identities per subscription. Eviction only
// drops cache ownership; queued samples retain their exact immutable identity.
class CaptureCache
{
public:
  CaptureCache(std::string scope, std::string rmw, size_t domain)
  : scope_(std::move(scope)), rmw_(std::move(rmw)), domain_(domain),
    enabled_(safe_scope(scope_) && rmw_ == "rmw_zenoh_cpp") {}

  IdentityPtr get(const uint8_t * gid, size_t size, uint64_t sequence)
  {
    if (!enabled_ || size != 16 || sequence == UINT64_MAX) {return nullptr;}
    std::array<uint8_t, 16> bytes;
    std::copy_n(gid, 16, bytes.begin());
    if (std::all_of(bytes.begin(), bytes.end(), [](uint8_t b) {return b == 0;})) {return nullptr;}
    const auto key = std::make_pair(bytes, static_cast<uint32_t>(sequence >> 32));
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = cache_.find(key);
    if (found != cache_.end()) {return found->second;}
    auto value = identity(publication_metadata(scope_, rmw_, domain_, gid, size, sequence));
    if (cache_.size() >= 128) {cache_.clear();}
    cache_.emplace(key, value);
    return value;
  }

private:
  const std::string scope_, rmw_;
  const size_t domain_;
  const bool enabled_;
  std::mutex mutex_;
  std::map<std::pair<std::array<uint8_t, 16>, uint32_t>, IdentityPtr> cache_;
};
}  // namespace rig_publication_provenance
#endif
