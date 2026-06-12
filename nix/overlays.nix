# nix/overlays.nix — Expose pkgs.hermes-agent for external NixOS configs
{ inputs, ... }:
{
  flake.overlays.default = final: _: {
    hermes-agent = final.callPackage ./hermes-agent.nix {
      inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
      rev = inputs.self.rev or null;
    };
  };
}
