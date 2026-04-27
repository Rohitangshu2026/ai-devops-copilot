# AI DevOps Copilot 🚀

## 📌 Overview

This project implements a **complete DevOps pipeline** with an AI-assisted debugging system.
It automates the Software Development Life Cycle (SDLC) from code push to deployment and monitoring.

---

## 🏗️ Architecture

```text
GitLab CI → Docker → Ansible → Kubernetes → ELK → AI Agent
```

---

## ⚙️ Tech Stack

* **Version Control:** GitLab
* **CI/CD:** GitLab CI/CD
* **Containerization:** Docker
* **Configuration Management:** Ansible
* **Orchestration:** Kubernetes
* **Logging & Monitoring:** ELK Stack (Elasticsearch, Logstash, Kibana)
* **Backend:** FastAPI (Python)

---

## 🚀 Features

* Automated CI/CD pipeline on every push
* Docker-based application deployment
* Kubernetes-based scalable infrastructure
* Centralized logging using ELK
* Modular Ansible roles for deployment
* Designed for AI-based failure analysis (future phase)

---

## 📁 Project Structure

```text
sample-app/   → FastAPI application
ansible/      → Deployment automation
k8s/          → Kubernetes manifests
elk/          → Logging configuration
```

---

## 🎯 Project Phases

* **Phase 1:** CI/CD + Deployment + Logging
* **Phase 2:** AI-based log analysis (read-only)
* **Phase 3:** Autonomous debugging & self-healing

---