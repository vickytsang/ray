// Copyright 2017 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Copyright (c) 2011 The LevelDB Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file. See the AUTHORS file for names of contributors.
//
// A Status encapsulates the result of an operation.  It may indicate success,
// or it may indicate an error with an associated error message.
//
// Multiple threads can invoke const methods on a Status without
// external synchronization, but if any of the threads may call a
// non-const method, all threads accessing the same Status must use
// external synchronization.

// Adapted from Apache Arrow, Apache Kudu, TensorFlow

#pragma once

#include <cstring>
#include <iosfwd>
#include <string>

#include "absl/strings/str_cat.h"
#include "ray/common/source_location.h"
#include "ray/util/logging.h"
#include "ray/util/macros.h"
#include "ray/util/visibility.h"

namespace boost::system {
class error_code;
}  // namespace boost::system

// Return the given status if it is not OK.
#define RAY_RETURN_NOT_OK(s)           \
  do {                                 \
    const ::ray::Status &_s = (s);     \
    if (RAY_PREDICT_FALSE(!_s.ok())) { \
      return _s;                       \
    }                                  \
  } while (0)

// If the status is not OK, CHECK-fail immediately, appending the status to the
// logged message. The message can be appended with <<.
#define RAY_CHECK_OK(s)                          \
  if (const ::ray::Status &_status_ = (s); true) \
  RAY_CHECK_WITH_DISPLAY(_status_.ok(), #s)      \
      << "Status not OK: " << _status_.ToString() << " "

namespace ray {

// If you add to this list, please also update kCodeToStr in status.cc.
enum class StatusCode : char {
  OK = 0,
  OutOfMemory = 1,
  KeyError = 2,
  TypeError = 3,
  Invalid = 4,
  IOError = 5,
  UnknownError = 9,
  NotImplemented = 10,
  RedisError = 11,
  TimedOut = 12,
  Interrupted = 13,
  IntentionalSystemExit = 14,
  UnexpectedSystemExit = 15,
  CreationTaskError = 16,
  NotFound = 17,
  Disconnected = 18,
  SchedulingCancelled = 19,
  AlreadyExists = 20,
  // object store status
  ObjectExists = 21,
  ObjectNotFound = 22,
  ObjectAlreadySealed = 23,
  ObjectStoreFull = 24,
  TransientObjectStoreFull = 25,
  // Object store is both out of memory and
  // out of disk.
  OutOfDisk = 28,
  ObjectUnknownOwner = 29,
  RpcError = 30,
  OutOfResource = 31,
  ObjectRefEndOfStream = 32,
  AuthError = 33,
  // Indicates the input value is not valid.
  InvalidArgument = 34,
  // Indicates that a channel (a mutable plasma object) is closed and cannot be
  // read or written to.
  ChannelError = 35,
  // Indicates that a read or write on a channel (a mutable plasma object) timed out.
  ChannelTimeoutError = 36,
  // If you add to this list, please also update kCodeToStr in status.cc.
};

#if defined(__clang__)
// Only clang supports warn_unused_result as a type annotation.
class RAY_MUST_USE_RESULT RAY_EXPORT Status;
#endif

class RAY_EXPORT Status {
 public:
  // Create a success status.
  Status() : state_(nullptr) {}
  ~Status() { delete state_; }

  Status(StatusCode code, const std::string &msg, int rpc_code = -1);
  Status(StatusCode code, const std::string &msg, SourceLocation loc, int rpc_code = -1);

  // Copy the specified status.
  Status(const Status &s);
  Status &operator=(const Status &s);

  // Move the specified status.
  Status(Status &&s);
  Status &operator=(Status &&s);

  // Return a success status.
  static Status OK() { return Status(); }

  // Return error status of an appropriate type.
  static Status OutOfMemory(const std::string &msg) {
    return Status(StatusCode::OutOfMemory, msg);
  }

  static Status KeyError(const std::string &msg) {
    return Status(StatusCode::KeyError, msg);
  }

  static Status ObjectRefEndOfStream(const std::string &msg) {
    return Status(StatusCode::ObjectRefEndOfStream, msg);
  }

  static Status TypeError(const std::string &msg) {
    return Status(StatusCode::TypeError, msg);
  }

  static Status UnknownError(const std::string &msg) {
    return Status(StatusCode::UnknownError, msg);
  }

  static Status NotImplemented(const std::string &msg) {
    return Status(StatusCode::NotImplemented, msg);
  }

  static Status Invalid(const std::string &msg) {
    return Status(StatusCode::Invalid, msg);
  }

  static Status IOError(const std::string &msg) {
    return Status(StatusCode::IOError, msg);
  }

  static Status InvalidArgument(const std::string &msg) {
    return Status(StatusCode::InvalidArgument, msg);
  }

  static Status RedisError(const std::string &msg) {
    return Status(StatusCode::RedisError, msg);
  }

  static Status TimedOut(const std::string &msg) {
    return Status(StatusCode::TimedOut, msg);
  }

  static Status Interrupted(const std::string &msg) {
    return Status(StatusCode::Interrupted, msg);
  }

  static Status IntentionalSystemExit(const std::string &msg) {
    return Status(StatusCode::IntentionalSystemExit, msg);
  }

  static Status UnexpectedSystemExit(const std::string &msg) {
    return Status(StatusCode::UnexpectedSystemExit, msg);
  }

  static Status CreationTaskError(const std::string &msg) {
    return Status(StatusCode::CreationTaskError, msg);
  }

  static Status NotFound(const std::string &msg) {
    return Status(StatusCode::NotFound, msg);
  }

  static Status Disconnected(const std::string &msg) {
    return Status(StatusCode::Disconnected, msg);
  }

  static Status SchedulingCancelled(const std::string &msg) {
    return Status(StatusCode::SchedulingCancelled, msg);
  }

  static Status AlreadyExists(const std::string &msg) {
    return Status(StatusCode::AlreadyExists, msg);
  }

  static Status ObjectExists(const std::string &msg) {
    return Status(StatusCode::ObjectExists, msg);
  }

  static Status ObjectNotFound(const std::string &msg) {
    return Status(StatusCode::ObjectNotFound, msg);
  }

  static Status ObjectUnknownOwner(const std::string &msg) {
    return Status(StatusCode::ObjectUnknownOwner, msg);
  }

  static Status ObjectAlreadySealed(const std::string &msg) {
    return Status(StatusCode::ObjectAlreadySealed, msg);
  }

  static Status ObjectStoreFull(const std::string &msg) {
    return Status(StatusCode::ObjectStoreFull, msg);
  }

  static Status TransientObjectStoreFull(const std::string &msg) {
    return Status(StatusCode::TransientObjectStoreFull, msg);
  }

  static Status OutOfDisk(const std::string &msg) {
    return Status(StatusCode::OutOfDisk, msg);
  }

  static Status RpcError(const std::string &msg, int rpc_code) {
    return Status(StatusCode::RpcError, msg, rpc_code);
  }

  static Status OutOfResource(const std::string &msg) {
    return Status(StatusCode::OutOfResource, msg);
  }

  static Status AuthError(const std::string &msg) {
    return Status(StatusCode::AuthError, msg);
  }

  static Status ChannelError(const std::string &msg) {
    return Status(StatusCode::ChannelError, msg);
  }

  static Status ChannelTimeoutError(const std::string &msg) {
    return Status(StatusCode::ChannelTimeoutError, msg);
  }

  static StatusCode StringToCode(const std::string &str);

  // Returns true iff the status indicates success.
  bool ok() const { return (state_ == nullptr); }

  bool IsOutOfMemory() const { return code() == StatusCode::OutOfMemory; }
  bool IsOutOfDisk() const { return code() == StatusCode::OutOfDisk; }
  bool IsKeyError() const { return code() == StatusCode::KeyError; }
  bool IsObjectRefEndOfStream() const {
    return code() == StatusCode::ObjectRefEndOfStream;
  }
  bool IsInvalid() const { return code() == StatusCode::Invalid; }
  bool IsIOError() const { return code() == StatusCode::IOError; }
  bool IsInvalidArgument() const { return code() == StatusCode::InvalidArgument; }
  bool IsTypeError() const { return code() == StatusCode::TypeError; }
  bool IsUnknownError() const { return code() == StatusCode::UnknownError; }
  bool IsNotImplemented() const { return code() == StatusCode::NotImplemented; }
  bool IsRedisError() const { return code() == StatusCode::RedisError; }
  bool IsTimedOut() const { return code() == StatusCode::TimedOut; }
  bool IsInterrupted() const { return code() == StatusCode::Interrupted; }
  bool IsIntentionalSystemExit() const {
    return code() == StatusCode::IntentionalSystemExit;
  }
  bool IsCreationTaskError() const { return code() == StatusCode::CreationTaskError; }
  bool IsUnexpectedSystemExit() const {
    return code() == StatusCode::UnexpectedSystemExit;
  }
  bool IsNotFound() const { return code() == StatusCode::NotFound; }
  bool IsDisconnected() const { return code() == StatusCode::Disconnected; }
  bool IsSchedulingCancelled() const { return code() == StatusCode::SchedulingCancelled; }
  bool IsAlreadyExists() const { return code() == StatusCode::AlreadyExists; }
  bool IsObjectExists() const { return code() == StatusCode::ObjectExists; }
  bool IsObjectNotFound() const { return code() == StatusCode::ObjectNotFound; }
  bool IsObjectUnknownOwner() const { return code() == StatusCode::ObjectUnknownOwner; }
  bool IsObjectAlreadySealed() const { return code() == StatusCode::ObjectAlreadySealed; }
  bool IsObjectStoreFull() const { return code() == StatusCode::ObjectStoreFull; }
  bool IsTransientObjectStoreFull() const {
    return code() == StatusCode::TransientObjectStoreFull;
  }

  bool IsRpcError() const { return code() == StatusCode::RpcError; }

  bool IsOutOfResource() const { return code() == StatusCode::OutOfResource; }

  bool IsAuthError() const { return code() == StatusCode::AuthError; }

  bool IsChannelError() const { return code() == StatusCode::ChannelError; }

  bool IsChannelTimeoutError() const { return code() == StatusCode::ChannelTimeoutError; }

  // Return a string representation of this status suitable for printing.
  // Returns the string "OK" for success.
  std::string ToString() const;

  // There's a [StatusString] for `StatusOr` also, used for duck-typed macro and template
  // to handle `Status`/`StatusOr` uniformly.
  std::string StatusString() const { return ToString(); }

  // Return a string representation of the status code, without the message
  // text or posix code information.
  std::string CodeAsString() const;

  StatusCode code() const { return ok() ? StatusCode::OK : state_->code; }

  int rpc_code() const { return ok() ? -1 : state_->rpc_code; }

  std::string message() const { return ok() ? "" : state_->msg; }

  template <typename... T>
  Status &operator<<(T &&...msg) {
    absl::StrAppend(&state_->msg, std::forward<T>(msg)...);
    return *this;
  }

 private:
  struct State {
    StatusCode code;
    std::string msg;
    SourceLocation loc;
    // If code is RpcError, this contains the RPC error code
    int rpc_code;
  };
  // Use raw pointer instead of unique pointer to achieve copiable `Status`.
  //
  // OK status has a `nullptr` state_.  Otherwise, `state_` points to
  // a `State` structure containing the error code and message(s)
  State *state_;

  void CopyFrom(const State *s);
};

static inline std::ostream &operator<<(std::ostream &os, const Status &x) {
  os << x.ToString();
  return os;
}

inline Status::Status(const Status &s)
    : state_((s.state_ == nullptr) ? nullptr : new State(*s.state_)) {}

inline Status &Status::operator=(const Status &s) {
  // The following condition catches both aliasing (when this == &s),
  // and the common case where both s and *this are ok.
  if (state_ != s.state_) {
    CopyFrom(s.state_);
  }
  return *this;
}

inline Status::Status(Status &&rhs) {
  state_ = rhs.state_;
  rhs.state_ = nullptr;
}

inline Status &Status::operator=(Status &&rhs) {
  if (this == &rhs) {
    return *this;
  }
  state_ = rhs.state_;
  rhs.state_ = nullptr;
  return *this;
}

Status boost_to_ray_status(const boost::system::error_code &error);

}  // namespace ray
