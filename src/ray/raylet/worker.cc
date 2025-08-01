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

#include "ray/raylet/worker.h"

#include <boost/bind/bind.hpp>
#include <memory>
#include <string>
#include <utility>

#include "ray/raylet/format/node_manager_generated.h"
#include "src/ray/protobuf/core_worker.grpc.pb.h"
#include "src/ray/protobuf/core_worker.pb.h"

namespace ray {

namespace raylet {

/// A constructor responsible for initializing the state of a worker.
Worker::Worker(const JobID &job_id,
               int runtime_env_hash,
               const WorkerID &worker_id,
               const Language &language,
               rpc::WorkerType worker_type,
               const std::string &ip_address,
               std::shared_ptr<ClientConnection> connection,
               rpc::ClientCallManager &client_call_manager,
               StartupToken startup_token)
    : worker_id_(worker_id),
      startup_token_(startup_token),
      language_(language),
      worker_type_(worker_type),
      ip_address_(ip_address),
      assigned_port_(-1),
      port_(-1),
      connection_(std::move(connection)),
      assigned_job_id_(job_id),
      runtime_env_hash_(runtime_env_hash),
      bundle_id_(std::make_pair(PlacementGroupID::Nil(), -1)),
      killing_(false),
      blocked_(false),
      client_call_manager_(client_call_manager) {}

rpc::WorkerType Worker::GetWorkerType() const { return worker_type_; }

void Worker::MarkDead() {
  bool expected = false;
  killing_.compare_exchange_strong(expected, true, std::memory_order_acq_rel);
}

bool Worker::IsDead() const { return killing_.load(std::memory_order_acquire); }

void Worker::KillAsync(instrumented_io_context &io_service, bool force) {
  bool expected = false;
  if (!killing_.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
    return;  // This is not the first time calling KillAsync or MarkDead, do nothing.
  }
  const auto worker = shared_from_this();
  if (force) {
    worker->GetProcess().Kill();
    return;
  }
#ifdef _WIN32
  // TODO(mehrdadn): implement graceful process termination mechanism
#else
  // Attempt to gracefully shutdown the worker before force killing it.
  kill(worker->GetProcess().GetId(), SIGTERM);
#endif

  auto retry_timer = std::make_shared<boost::asio::deadline_timer>(io_service);
  auto timeout = RayConfig::instance().kill_worker_timeout_milliseconds();
  auto retry_duration = boost::posix_time::milliseconds(timeout);
  retry_timer->expires_from_now(retry_duration);
  retry_timer->async_wait(
      [timeout, retry_timer, worker](const boost::system::error_code &error) {
#ifdef _WIN32
#else
        if (worker->GetProcess().IsAlive()) {
          RAY_LOG(INFO) << "Worker with PID=" << worker->GetProcess().GetId()
                        << " did not exit after " << timeout
                        << "ms, force killing with SIGKILL.";
        } else {
          return;
        }
#endif
        // Force kill worker
        worker->GetProcess().Kill();
      });
}

void Worker::MarkBlocked() { blocked_ = true; }

void Worker::MarkUnblocked() { blocked_ = false; }

bool Worker::IsBlocked() const { return blocked_; }

WorkerID Worker::WorkerId() const { return worker_id_; }

Process Worker::GetProcess() const { return proc_; }

StartupToken Worker::GetStartupToken() const { return startup_token_; }

void Worker::SetProcess(Process proc) {
  RAY_CHECK(proc_.IsNull());  // this procedure should not be called multiple times
  proc_ = std::move(proc);
}

void Worker::SetStartupToken(StartupToken startup_token) {
  startup_token_ = startup_token;
}

Language Worker::GetLanguage() const { return language_; }

const std::string Worker::IpAddress() const { return ip_address_; }

int Worker::Port() const {
  // NOTE(kfstorm): Since `RayletClient::AnnounceWorkerPort` is an asynchronous
  // operation, the worker may crash before the `AnnounceWorkerPort` request is received
  // by raylet. In this case, Accessing `Worker::Port` in
  // `NodeManager::ProcessDisconnectClientMessage` will fail the check. So disable the
  // check here.
  // RAY_CHECK(port_ > 0);
  return port_;
}

int Worker::AssignedPort() const { return assigned_port_; }

void Worker::SetAssignedPort(int port) { assigned_port_ = port; };

void Worker::AsyncNotifyGCSRestart() {
  if (rpc_client_) {
    rpc::RayletNotifyGCSRestartRequest request;
    rpc_client_->RayletNotifyGCSRestart(request, [](Status status, auto reply) {
      if (!status.ok()) {
        RAY_LOG(ERROR) << "Failed to notify worker about GCS restarting: "
                       << status.ToString();
      }
    });
  } else {
    notify_gcs_restarted_ = true;
  }
}

void Worker::Connect(int port) {
  RAY_CHECK(port > 0);
  port_ = port;
  rpc::Address addr;
  addr.set_ip_address(ip_address_);
  addr.set_port(port_);
  rpc_client_ = std::make_unique<rpc::CoreWorkerClient>(addr, client_call_manager_, []() {
    RAY_LOG(FATAL) << "Raylet doesn't call any retryable core worker grpc methods.";
  });
  Connect(rpc_client_);
}

void Worker::Connect(std::shared_ptr<rpc::CoreWorkerClientInterface> rpc_client) {
  rpc_client_ = rpc_client;
  if (notify_gcs_restarted_) {
    // We need to send RPC to notify about the GCS restarts
    AsyncNotifyGCSRestart();
    notify_gcs_restarted_ = false;
  }
}

void Worker::AssignTaskId(const TaskID &task_id) {
  assigned_task_id_ = task_id;
  if (!task_id.IsNil()) {
    task_assign_time_ = absl::Now();
  }
}

const TaskID &Worker::GetAssignedTaskId() const { return assigned_task_id_; }

const JobID &Worker::GetAssignedJobId() const { return assigned_job_id_; }

std::optional<bool> Worker::GetIsGpu() const { return is_gpu_; }

std::optional<bool> Worker::GetIsActorWorker() const { return is_actor_worker_; }

int Worker::GetRuntimeEnvHash() const { return runtime_env_hash_; }

void Worker::AssignActorId(const ActorID &actor_id) {
  RAY_CHECK(actor_id_.IsNil())
      << "A worker that is already an actor cannot be assigned an actor ID again.";
  RAY_CHECK(!actor_id.IsNil());
  actor_id_ = actor_id;
}

const ActorID &Worker::GetActorId() const { return actor_id_; }

const std::string Worker::GetTaskOrActorIdAsDebugString() const {
  std::stringstream id_ss;
  if (GetActorId().IsNil()) {
    id_ss << "task ID: " << GetAssignedTaskId();
  } else {
    id_ss << "actor ID: " << GetActorId();
  }
  return id_ss.str();
}

bool Worker::IsDetachedActor() const {
  return assigned_task_.GetTaskSpecification().IsDetachedActor();
}

const std::shared_ptr<ClientConnection> Worker::Connection() const { return connection_; }

void Worker::SetOwnerAddress(const rpc::Address &address) { owner_address_ = address; }
const rpc::Address &Worker::GetOwnerAddress() const { return owner_address_; }

void Worker::ActorCallArgWaitComplete(int64_t tag) {
  RAY_CHECK(port_ > 0);
  rpc::ActorCallArgWaitCompleteRequest request;
  request.set_tag(tag);
  request.set_intended_worker_id(worker_id_.Binary());
  rpc_client_->ActorCallArgWaitComplete(
      request, [](Status status, const rpc::ActorCallArgWaitCompleteReply &reply) {
        if (!status.ok()) {
          RAY_LOG(ERROR) << "Failed to send wait complete: " << status.ToString();
        }
      });
}

const BundleID &Worker::GetBundleId() const { return bundle_id_; }

void Worker::SetBundleId(const BundleID &bundle_id) { bundle_id_ = bundle_id; }

void Worker::SetJobId(const JobID &job_id) {
  if (assigned_job_id_.IsNil()) {
    assigned_job_id_ = job_id;
  }

  RAY_CHECK(assigned_job_id_ == job_id)
      << "Job_id mismatch, assigned: " << assigned_job_id_.Hex()
      << ", actual: " << job_id.Hex();
}

void Worker::SetIsGpu(bool is_gpu) {
  if (!is_gpu_.has_value()) {
    is_gpu_ = is_gpu;
  }
  RAY_CHECK_EQ(is_gpu_.value(), is_gpu)
      << "is_gpu mismatch, assigned: " << is_gpu_.value() << ", actual: " << is_gpu;
}

void Worker::SetIsActorWorker(bool is_actor_worker) {
  if (!is_actor_worker_.has_value()) {
    is_actor_worker_ = is_actor_worker;
  }
  RAY_CHECK_EQ(is_actor_worker_.value(), is_actor_worker)
      << "is_actor_worker mismatch, assigned: " << is_actor_worker_.value()
      << ", actual: " << is_actor_worker;
}

}  // namespace raylet

}  // end namespace ray
