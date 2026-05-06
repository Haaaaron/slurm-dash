//! Snapshot tar reading via SSH + file tree builder.
//! Mirrors `_build_file_tree` in `web/routes.py` and the `tar -tzf` /
//! `tar -xzOf | head -c` invocations.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use anyhow::Result;
use once_cell::sync::Lazy;
use regex::Regex;
use shell_escape::unix::escape;

use crate::models::TreeNode;
use crate::ssh::{run_ssh, SshOutput};

pub async fn list_snapshot(ssh_string: &str, snapshot_path: &str) -> Result<SshOutput> {
    let cmd = format!("tar -tzf {}", escape(snapshot_path.into()));
    run_ssh(ssh_string, &cmd, Duration::from_secs(15)).await
}

pub async fn read_snapshot_file(
    ssh_string: &str,
    snapshot_path: &str,
    inner_path: &str,
) -> Result<SshOutput> {
    let cmd = format!(
        "tar -xzOf {} {} | head -c 262144",
        escape(snapshot_path.into()),
        escape(inner_path.into())
    );
    run_ssh(ssh_string, &cmd, Duration::from_secs(15)).await
}

#[derive(Debug)]
struct Node {
    name: String,
    path: String,
    is_dir: bool,
    parent: String,
    child_keys: BTreeSet<String>,
}

pub fn build_file_tree(paths: &[String]) -> Vec<TreeNode> {
    let mut nodes: BTreeMap<String, Node> = BTreeMap::new();

    for raw in paths {
        let is_dir = raw.ends_with('/');
        let mut clean: &str = raw.trim_end_matches('/');
        if let Some(stripped) = clean.strip_prefix("./") {
            clean = stripped;
        }
        if clean.is_empty() || clean == "." {
            continue;
        }
        let parts: Vec<&str> = clean.split('/').collect();

        for depth in 0..parts.len() {
            let key = parts[..=depth].join("/");
            let is_node_dir = depth < parts.len() - 1 || is_dir;
            let name = parts[depth].to_string();
            let parent = if depth == 0 {
                String::new()
            } else {
                parts[..depth].join("/")
            };
            let entry = nodes.entry(key.clone()).or_insert_with(|| Node {
                name,
                path: key.clone(),
                is_dir: is_node_dir,
                parent,
                child_keys: BTreeSet::new(),
            });
            if is_node_dir {
                entry.is_dir = true;
            }
        }

        for depth in 1..parts.len() {
            let p = parts[..depth].join("/");
            let c = parts[..=depth].join("/");
            if let Some(parent_node) = nodes.get_mut(&p) {
                parent_node.child_keys.insert(c);
            }
        }
    }

    fn make_node(nodes: &BTreeMap<String, Node>, key: &str) -> TreeNode {
        let n = &nodes[key];
        let mut dirs: Vec<&str> = n
            .child_keys
            .iter()
            .filter(|k| nodes.get(k.as_str()).map(|c| c.is_dir).unwrap_or(false))
            .map(|s| s.as_str())
            .collect();
        let mut files: Vec<&str> = n
            .child_keys
            .iter()
            .filter(|k| !nodes.get(k.as_str()).map(|c| c.is_dir).unwrap_or(false))
            .map(|s| s.as_str())
            .collect();
        dirs.sort_by_key(|k| nodes[*k].name.to_lowercase());
        files.sort_by_key(|k| nodes[*k].name.to_lowercase());
        let mut children: Vec<TreeNode> = Vec::new();
        for k in dirs.iter().chain(files.iter()) {
            children.push(make_node(nodes, k));
        }
        TreeNode {
            name: n.name.clone(),
            path: if n.is_dir { String::new() } else { n.path.clone() },
            is_dir: n.is_dir,
            is_submit_script: n.path == "submit_script.sh" && !n.is_dir,
            children,
        }
    }

    let root_keys: Vec<String> = nodes
        .iter()
        .filter(|(_, v)| v.parent.is_empty())
        .map(|(k, _)| k.clone())
        .collect();
    let mut dirs: Vec<&String> = root_keys
        .iter()
        .filter(|k| nodes[k.as_str()].is_dir)
        .collect();
    let mut files: Vec<&String> = root_keys
        .iter()
        .filter(|k| !nodes[k.as_str()].is_dir)
        .collect();
    dirs.sort_by_key(|k| nodes[k.as_str()].name.to_lowercase());
    files.sort_by_key(|k| nodes[k.as_str()].name.to_lowercase());

    dirs.into_iter()
        .chain(files)
        .map(|k| make_node(&nodes, k.as_str()))
        .collect()
}

static ENV_VAR_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))").unwrap()
});

pub fn expand_env(text: &str, env: &serde_json::Value) -> String {
    let env_obj = env.as_object();
    ENV_VAR_RE
        .replace_all(text, |caps: &regex::Captures| {
            let name = caps
                .get(1)
                .or_else(|| caps.get(2))
                .map(|m| m.as_str())
                .unwrap_or("");
            if let Some(obj) = env_obj {
                if let Some(v) = obj.get(name).and_then(|v| v.as_str()) {
                    return v.to_string();
                }
            }
            caps.get(0).unwrap().as_str().to_string()
        })
        .into_owned()
}
