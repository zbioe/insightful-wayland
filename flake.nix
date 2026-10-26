{
  description = "Insightful (Workpuls) on a Wayland desktop with mango, as a home-manager module";

  outputs = _: {
    homeManagerModules.default = ./module.nix;
  };
}
